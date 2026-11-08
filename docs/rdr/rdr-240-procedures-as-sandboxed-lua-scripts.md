---
title: "Procedures as Sandboxed Lua Scripts: Run the Fixed Sequence Instead of Recalling It"
id: RDR-240
type: Feature
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-01
accepted_date: 2026-11-07
related_issues: [nexus-qo7ie.2, nexus-mdbds.1]
related_rdrs: [RDR-238, RDR-242, RDR-239, RDR-209, RDR-111, RDR-190]
---

# RDR-240: Procedures as Sandboxed Lua Scripts: Run the Fixed Sequence Instead of Recalling It

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Split from RDR-238 on 2026-10-31 at Sam's review: its Gaps 3
and 5 (procedures stored as memory, and procedures recalled instead of run)
move here, and the procedures become sandboxed Lua scripts rather than new
Python verbs or MCP tools. The starting text is RDR-238 as filed at commit
740a7a3cd (Gaps 3 and 5, Technical Design 7 to 11, Phases 3 and 5). This RDR
is the first consumer of the Lua runtime of RDR-242 (nexus-mdbds.1, accepted
2026-11-07), and was revised on 2026-11-07 to that RDR as accepted: no write
verb in its first version, its verb list, its limits, and the header format,
which RDR-242 leaves to this RDR (its gate residuals S3 and S4, on
nexus-mdbds).

## Problem Statement

Nexus keeps several of its procedures as memory: markdown files that an
agent reads and then carries out from its own reading. Claude Code's file
memory (`~/.claude/projects/<repo>/memory/`, one rule per file, listed in an
always-loaded index, `MEMORY.md`) is meant for rules. Some of the files in it
are long procedures, and some of those procedures have one right answer.
Reading a fixed sequence and then performing it is where it goes wrong: the
step is misread, skipped or improvised, and the rule against the mistake is
in the same memory that failed to prevent it.

### Enumerated gaps to close

#### Gap 1: Procedures are stored as memory

The largest memory files are procedures, not rules (sizes read 2026-11-01):
`feedback_test_runs_before_push.md` (33 KB), `feedback_release_and_develop_ci.md`
(27 KB), `feedback_hand_kept_enumerations.md` (22 KB) and
`feedback_destructive_action_guards.md` (14 KB). They are read when someone
remembers to read them, and each index line has to summarise a whole
procedure. "Lifelong Context Adaptation through Bidirectional Memory–Skill
Learning" is the one paper in the survey that tests moving memory into
procedures: memory and skills together reached 63.2% on ALFWorld against
50.5% for skills alone, so the source episodes stay as evidence, and
admission is gated. No nexus path moves a memory into a skill, a script or a
hook. The fix delivers a rule for which form a procedure takes, and a loss
check before the source file shrinks.

#### Gap 2: A procedure with one right answer is recalled or improvised instead of run

A skill is still text that a model reads and then carries out. For a
procedure with one right answer the safe form is code that does the work.
Evidence from the session that wrote RDR-238 (2026-10-31):

- Six transcript scans were written by hand as throwaway heredocs: the
  subagent model mix and cost, plugin and tool use, memory-file reads, a
  rule-violation baseline, the auto-memory census by Claude Code version, and
  gate-record title mapping. RDR-238's Phase 2 and its curation report need
  the same scans again. `scripts/skill_routing_census.py` (nexus-dg7i6.1) is
  one such instrument already, kept as a script for one question.
- `nx rdr preamble rdr-research -- add` takes the finding as bare shell
  arguments (the `rdr-research` skill's own usage line, read 2026-11-01:
  `add <id> ... <finding text tokens...>`). A finding with semicolons was
  split by the shell into commands, one of which ran as `git stash 8
  sessions...` (git refused it), and three records were truncated and had to
  be rewritten. A memory file names this exact hazard
  (`feedback_rdr_research_add_text_is_shell_parsed.md`); the rule was in
  memory and did not prevent it.
- Gate-record tags (nexus-qo7ie.1) are free text inside a `memory_put` call,
  so an agent can misspell or omit them, and the RDR-209 rollup inherits
  whatever was typed.
- `feedback_test_runs_before_push.md` is a decision procedure for which tests
  to run before a push. Two selectors already compute part of it
  (`scripts/select_affected_integration_tests.py`,
  `scripts/select_docs_reader_tests.py`); the rest is recalled.

The harness survey points the same way: in its ablation, tools (+3.3 points)
and middleware (+2.2) carried the gain, and the system prompt alone made
results worse (Harness Engineering, p.66). The fix delivers four procedures
as scripts that run.

#### Gap 3: Each new command costs code, a schema and upkeep

The pre-split design answered Gap 2 with four new Python verbs (`nx
transcripts`, `nx gate record`, `nx preflight`, stdin on three verbs), each
with an MCP twin chosen by a front-door rule. Every one of those is engine or
client code, a CLI surface, often an MCP tool schema that sits in every
context as a deferred name, and a release to change. Over the 30 days to
2026-10-31, sessions made about 21,000 nexus MCP tool calls and about 1,200
`nx` CLI calls (RDR-238 survey), so the MCP surface is the hot one, and
every tool added to it is paid for in every session. Sam's direction is that
procedures should be data run by one host, not a verb each. The fix
delivers one runner with one front door per caller kind, and a library of
scripts behind it.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-238 Memory Curation | Parent; split from it | It owns supersession by deletion, the bounded index and the curation habits. Its curation report's cold-state section takes MCP tool, CLI verb and injected-context counts from this RDR's transcript queries (RDR-238 Technical Design 4); its round-1 gate (2026-11-01) blocked on that dependency, and its fix made this RDR a prerequisite of its Phase 2 and of `nx memory curate`, calling with the argument object `{query, since, until, project}` the `memory`, `tools`, `loads` and `violations` queries (RDR-238 Technical Design 5). |
| RDR-242 Sandboxed Lua Runtime (nexus-mdbds.1, accepted 2026-11-07) | Depends on it | It owns the sandbox: the interpreter (`lupa`, PUC Lua 5.4), the worker process, the static check, the verb table, the limits, the library lookup and the shadowing refusal (its Technical Design 2 to 13). Its version 1 has no write verb; one waits for an approval document (its Technical Design 1 and 10). This RDR owns the procedure header that RDR-242 reads the grants from (Technical Design 1 here; RDR-242 residual S4), the argument check, the procedures and their queries, and states what the queries need from `transcripts.sessions` (Technical Design 4 here). Whether a repo root runs by default is a question RDR-242's round-3 gate left open for Sam (its residual S5, on nexus-mdbds; RDR-242 as accepted runs both roots). Three of the four procedures here ship in the package root; `preflight` is nexus's own and lives in the nexus repo's root (Technical Design 8), so it alone depends on the answer. |
| RDR-239 The Tuple-Space Dictionary | Sibling in the same epic (nexus-mdbds) | Stage 1 of the scriptable tuple space. The runtime exposes what the dictionary describes; these procedures read memory and transcripts, not tuples, so they use no dictionary entry in v1. |
| RDR-209 Heat-Based Graduation | Consumer | Its gate-record rollup reads the tags the gate-record script derives. Until its Phase 0b read counter lands, the transcript queries here are its read-count source. |
| RDR-111 The ORB | Abandoned; lesson kept | Its arc (RDR-110 to 119) built a composition layer ahead of its consumers and was scrapped. This RDR is the runtime's first consumer, and ships one procedure at a time. |
| RDR-190 Plan-IR `loop` and `collect` | Deferred; its rejection is narrower than it looks | It rejected a script sandbox ("code mode") for plans on Parmar's grounds against code a model generates at run time: sandboxing, inspectability, schema validation, and models emitting JSON more reliably than code. The scripts here are written by hand, reviewed and committed like any source file; a model never writes one at run time. It emits only a procedure name and a JSON argument object, which is the blueprint form RDR-190 kept. |

Grepped `docs/rdr/` for `lua` and `sandbox` (2026-11-01). The Lua hits outside
RDR-238 and RDR-239 name Lua as a chunker language (RDR-016, RDR-014, RDR-028);
the sandbox hits outside RDR-190 concern containers, test sandboxes and MCP's
HTML resources. The six rows above are the ones with real overlap.

## Context

### Background

RDR-238's memory survey (2026-10-31, 5,285 sessions over 30 days) found that
file memory is written far more than it is read (225 writes, 68 reads) and
that the index carries most of the value. The four largest files are the
procedures listed in Gap 1. Sam's review of RDR-238 the same day split the
procedures out and set their form: sandboxed Lua scripts, on a runtime that
is also stage 3 of the scriptable tuple space (nexus-mdbds). The model Sam
named is Augur: Lua over a sanctioned API, with rules the host enforces.

### Technical Environment

- No Lua interpreter is a nexus dependency today (`lupa` is absent from
  `pyproject.toml` and `uv.lock`, and `src/nexus/lua_runtime/` does not
  exist, checked 2026-11-07). RDR-242 chose `lupa.lua54` through
  `lupa>=2.8,<3` (its Technical Design 5); its Phase 1 adds it.
- `nx memory put` already reads its content from stdin when given `-`
  (`nx memory put --help`, 2026-11-01). `nx rdr preamble rdr-research -- add`
  does not.
- Closed vocabularies live as checked tables in `src/nexus/tables/`.
- Skills for this repo live in conexus (`conexus/skills/`, shipped to every
  conexus user) and in `.claude/skills/` (repo-local).

## Research Findings

### Investigation

The evidence in Gap 2 comes from one session's transcript and the
`rdr-research` skill's usage line. The file sizes in Gap 1 and the
environment facts were read on 2026-11-01. The paper results come from
RDR-238's survey passes (T2 `nexus/memory-survey-papers-agent-memory-2026-10-31`,
`nexus/memory-survey-papers-context-load-2026-10-31`). The runtime facts
come from RDR-242 as accepted, read 2026-11-07 (`nexus_rdr/240-research-3`),
and from its round-3 gate critique, which measured the memory fit and named
the header question (`nexus_rdr/240-research-4`, `-5`).

### Key Discoveries

- **Verified (paper):** promoted skills work alongside their sources, not
  instead of them, and short procedural skills beat exhaustive prose
  (Bidirectional Memory–Skill Learning, Table 4a; SkillsBench, Finding 6).
- **Verified (paper):** unvalidated compaction can fall below the no-context
  baseline: one compression from 18,282 to 122 tokens dropped accuracy from
  66.7% to 57.1% (Agentic Context Management, p.6). A promotion that shrinks
  its source needs a loss check.
- **Verified (local):** the rule against shell-parsed findings was in memory
  when the shell split a finding (Gap 2). Memory did not prevent the
  failure; the argument form caused it.
- **Verified (spike, RDR-238 research 4):** over 188 main sessions in 14
  days, a backtick inside a quoted `nx` or `bd` argument appeared in 2
  sessions (1.1 per 100). It is one of the failures prose-as-a-string
  removes.

### Critical Assumptions

- **A1:** The runtime of nexus-mdbds.1 can expose named host verbs that read
  transcripts, T2 memory and the git diff, and can grant one named write verb
  (`memory.put`) to a script that declares it. Verification: that RDR's
  design. If the runtime stays read-only with no write grant, the
  gate-record and research-finding scripts return the record and the caller
  writes it with `memory_put`. **Settled 2026-11-07, the fallback:** RDR-242
  ships the reads and no write verb, and a write verb waits for its approval
  document (`nexus_rdr/240-research-3`). This RDR is designed on the
  fallback.
- **A2:** Transcript queries written in Lua over host-parsed records are
  fast enough and fit in memory: a 30-day query over about 5,000 sessions
  finishes in under a minute on the laptop, and each verb result fits the
  worker. RDR-242's limits bound both: 60 s of wall time with verb time
  counted, and about 22 MB of JSON per verb result before 64 MB of Lua memory
  is full (measured 2.8 times JSON size for session-like records;
  `nexus_rdr/240-research-4`). Verification: Phase 1 Step 1's spike, timed
  and sized, with its pass lines there.
- **A3:** Agents call a script when one exists instead of improvising.
  Verification: the transcript query `procedures` counts script runs against
  hand-written equivalents (heredoc transcript scans, `memory_put` calls
  carrying gate tags) for 30 days after each script lands.
- **A4:** The four largest procedural files can move into a skill, a script
  or a hook without losing a rule. Verification: Phase 3's loss check on
  each one.

## Proposed Solution

### Approach

Choose the form by what the procedure holds; run the fixed ones as scripts;
pass prose as data, never as shell words.

**Which form a procedure takes (Gaps 1 and 3).** A memory file is a
candidate to move when its "How to apply" is a procedure (ordered steps, or
more than about 8 KB) or a bright line. Its target depends on what it holds:

| The memory holds | Target | Why | Example |
| --- | --- | --- | --- |
| A procedure that needs judgment | Skill | Loads on demand; the model applies it | release checklist, review gate |
| A fixed sequence with one right answer | Lua script in a procedure library | Runs the same way every time; nothing to misremember | what to check before a push |
| A bright line that must always hold | Hook | Claude Code's docs: memory is context, a hook is enforcement | no `git stash`, no full-disk `find` |

A nexus-only procedure goes in the repo's library; a practice every conexus
user should follow ships in the plugin's library; Sam's personal rules stay
as memory. The target cites its source files, and the source shrinks to the
rule and its why, plus a pointer to the skill, script or hook.

**One runner, two doors (Gap 3).** A script is run by one host function.
Two thin wrappers call it: an MCP tool, `procedure_run(name, args)`, for
agents, and a CLI verb, `nx run <name> [--args-file F | -]`, for people and
CI. Arguments are a JSON object in both. Prose passes as a string inside
that object, so no finding, gate body or bead text ever passes through shell
parsing. Adding a procedure adds a file to a library, not a verb or a tool
schema.

**The first four procedures (Gap 2).** In order of payoff:

1. `transcripts`: a library of named, read-only queries over Claude Code
   session transcripts. It absorbs `scripts/skill_routing_census.py` and the
   six scans in Gap 2, and is the measurement source for RDR-238's Phase 2,
   its curation report, and RDR-209's heat work until its read counter
   lands.
2. `gate_record`: builds a gate record from structured fields and derives
   its title and tags, so no agent composes a tag. The runtime has no write
   verb (A1), so it returns the record and the caller writes it unchanged
   with `memory_put`.
3. `research_add`: builds an RDR research record from a string, which the
   caller writes the same way, replacing the bare-argument form of `nx rdr
   preamble rdr-research -- add`.
4. `preflight`: reads the diff, names its change classes, and returns the
   checks each class needs as commands, the existing selectors included.

### Technical Design

1. **Procedure library and header.** One Lua file per procedure, named
   `<name>.lua`. Two library roots: `src/nexus/procedures/` (ships with `nx`,
   reaches every conexus user) and `.nexus/procedures/` in a repo (that
   repo's own procedures). A repo procedure may not shadow a shipped one;
   RDR-242's `run_script` refuses the name (its Technical Design 4). This RDR
   owns the header format, and it is decided once here (RDR-242 residual S4,
   `nexus_rdr/240-research-5`). The header is the file's first four lines,
   in this order, each a Lua comment, so Lua compiles none of it:

   ```lua
   -- procedure: research_add
   -- summary: Build an RDR research record; the caller writes it with memory_put.
   -- args: rdr:integer, classification:string, method:string, finding:string, repo:string
   -- grants: memory.list, tables.get
   ```

   - `procedure` equals the file's base name.
   - `summary` is one line of at most 100 characters; `nx run --list` and the
     MCP tool's description show it.
   - `args` is `none`, or a comma-separated list of `name:type`, each name
     matching `[a-z][a-z0-9_]{0,31}` and used once, each type one of
     `string`, `integer`, `number` and `boolean`, with a trailing `?` for an
     optional argument.
   - `grants` is `none`, or a comma-separated list of verb names.
   - Anything else in those four lines, a missing line, or a key out of
     order refuses the file with the kind `header`, naming the line.

   One Python function, `nexus.lua_runtime.header.parse(source) -> Header`,
   reads it as text and is the only reader. It lives with the runtime, so
   the runtime imports nothing from the procedures, and RDR-242's Phase 3
   Step 3, the step its residual S4 names, builds it to this format. The
   spike's reader (`static_check.py` lines 238-245) scans three lines; the
   port reads four. The parser also refuses a header byte outside printable
   ASCII or tab; the static check's `bytes` rule allows printable ASCII,
   tab, CR and LF in the whole file, and the header's lines are split
   before the check.
   RDR-242's `run_script` calls it on the bytes it read, before the static
   check, which takes the grants from it and cross-checks them against the
   bytecode (its rules `verb-unknown`, `verb-ungranted` and
   `grant-unused`). The same call checks the caller's `args` against it
   (Technical Design 2), so the file is read and parsed once per run, inside
   `run_script`, before any worker starts. A header or argument refusal is
   RDR-242's `ScriptError` with the kind `header` or `args`. Both are
   requirements on `run_script` beyond RDR-242's Technical Design 4, which
   lists only the name, root and static-check refusals; they are recorded on
   nexus-mdbds with its residual S4.
2. **Runner.** One Python function, `run_procedure(name, args)`, calls
   RDR-242's `run_script(name, args)` and returns its JSON result. It reads
   no file and checks nothing itself. The argument check runs inside
   `run_script` (Technical Design 1) and refuses with the kind `args`,
   naming the argument: an argument the header does not name, a missing
   required argument, or a JSON value of the wrong type (an `integer` is a
   JSON number with no fractional part). Two argument names are reserved,
   because the sandbox has no clock and no verb returns the repo (RDR-242
   Technical Design 7 and 10, `nexus_rdr/240-research-6`): `now`, the host's
   UTC time as an ISO 8601 string, and `repo`, the worktree-stable repo name
   that `nx rdr` already uses for `<repo>_rdr` (the main checkout's
   basename through `nexus.repo_identity._resolve_main_repo`, as
   `_gate_repo_name` in `src/nexus/commands/rdr.py` lines 1040-1056 does,
   so a linked worktree gives the main checkout's name), absent outside a
   repo. `run_script`, which holds the parsed header, adds each reserved
   value only when the header declares it, and refuses with the kind `args`
   a caller that passes either; this is a third requirement on
   `run_script`, recorded on nexus-mdbds with the two of Technical Design 1.
   The clock it reads is injectable, so a test fixes `now`. RDR-242 owns the rest: the lookup,
   the name rule, the static check, the grants, the limits, and the errors
   (`ScriptError`, `ScriptLimitError`, `ScriptWorkerError`,
   `ScriptBusyError`), which `run_procedure` passes through unchanged. No
   procedure writes in version 1 (A1), so a failed run leaves nothing to undo.
3. **Front doors.** The MCP tool `procedure_run` and the CLI verb `nx run`
   both call `run_procedure` and nothing else. `nx run --list` and the MCP
   tool's description list the library's procedures with their header's
   `summary`. An error comes back as its kind and detail: the MCP tool
   returns it as text, and `nx run` prints it and exits nonzero.
4. **Host verbs the four procedures use.** RDR-242's version 1 verbs,
   exactly (its Technical Design 10): `memory.get(project, title)`,
   `memory.list(project)`, `memory.search(query, project, limit)`,
   `transcripts.sessions(since, until, project, bodies, fields)`,
   `git.diff_names(base)`, `git.diff_stat(base)` and `tables.get(name)`. No
   write verb. RDR-242 builds `transcripts.sessions` as a new client
   function; this RDR states what its records carry:
   - one record per session or subagent transcript, parsed in Python, with
     its session id, whether it is a main session, its tool calls with their
     inputs (a `memory_get` title, a Bash command) and times, instruction
     loads, model, token counts and sizes. A tool call's input is not a
     message body.
   - the names of the tools deferred to the session (announced, not
     loaded), and on each tool call an `is_error` flag read from its
     result's error marker, without the result's content; `tools` needs the
     first and `agents` the second.
   - a session belongs to the window that holds its first record's time (its
     start), and to no other, so a session that runs past midnight, or is
     resumed on a later day, is counted once.
   - no message bodies unless the caller passes `bodies=true`, since
     transcripts hold everything a session saw. With `bodies=true` a record
     carries the session's assistant text, and never tool results (what a
     session read), which no query needs: the `violations` detectors scan
     assistant text and Bash inputs (RDR-238 Phase 2 Step 1).
   - a record over 1 MB of JSON comes back with its tool-call inputs and
     assistant text cut to fit and `truncated: true`, so one large session
     cannot end a run; a query counts truncated records and reports the
     count in its result.
   - `since` and `until` are UTC dates or timestamps, so a query can ask
     for one day or one hour.
   - for the rows of Technical Design 5, each instruction load carries its
     type and its size in bytes (never its content), and the record carries
     its assistant turns (distinct API messages), the first turn's context
     tokens, and the names of the slash commands the user ran, read only
     from the prompts the harness writes for them, never from prompt text.
     A record that still does not fit once its strings are cut loses the
     tails of its lists, each counted.
5. **`transcripts`.** Queries, each a function in the script with a fixture
   test, called with the argument object `{query, since, until, project}`
   (`since` and `until` UTC dates or timestamps, `until` defaulting to the reserved `now`
   of Technical Design 2, `project` a
   Claude Code project directory name or `all`). Rows of `memory`, `tools`
   and `loads` carry a session id, a name, a count, a last-use time and a
   size; rows of `violations` carry a rule, a count and the number of main
   sessions in the window (RDR-238 Technical Design 5):
   `agents` (dispatches by agent type and model, starting context,
   turns, errors, estimated cost), `tools` (calls per tool and MCP server,
   deferred against called, and per `nx` CLI verb called through Bash, read
   from the Bash command text inside the procedure, which RDR-238's
   cold-state report needs), `skills` (the skill-routing census, ported),
   `memory` (memory-file reads and writes, T2 reads per title
   from `memory_get` calls and per project from `memory_search` calls, which
   carry no title, and what loaded into a subagent's
   instructions), `violations` (named rule detectors, counted per 100 main
   sessions and reading message bodies inside the procedure, RDR-238's Phase
   2 before and after counts), `loads` (instruction files, their types and
   sizes, per main session and per dispatch), and
   `procedures` (script runs against hand-written equivalents, A3). Cost
   uses one dated price table and says it is an estimate.

   Every query calls `transcripts.sessions` one UTC day at a time, stepping
   days with a small inline function (the sandbox has no `require` and no
   clock), and keeps only its rows between calls, so no verb result holds
   more than a day's records; 30 days are 30 verb calls, against RDR-242's
   cap of 10,000 a run. A run's own result is capped at 1 MB of JSON
   (RDR-242 Technical Design 11), and the per-session rows of `memory`,
   `tools` and `loads` over 30 days are larger than that (inferred, not
   measured, from about 5,000 sessions; round-1 gate critique C1). So the
   result is paged: a run adds whole days while its rows stay under 768 KB
   and returns `{rows, truncated_records, until, next_since}`. `until` is the
   value the run used, the reserved `now` when the caller gave none, so the
   caller can repeat it. `next_since` is where the next page starts, a UTC
   date or, after a day split into hours, a timestamp, and it is absent once
   the window is done. The caller runs the procedure again with `since` set
   to `next_since` and the returned `until`, and joins the pages: per-session
   rows (`memory`, `tools`, `loads`) concatenate; the keyed rows of the other
   four sum by key (`violations` by rule, its main-session count included;
   `agents` by agent type and model; `skills` by skill; `procedures` by
   procedure); `truncated_records` sums. The sandbox has no JSON encoder, so
   the script estimates a row's size as twice the byte length of its strings
   plus 16 bytes a field; the 256 KB between 768 KB and RDR-242's 1 MB cap is
   the margin for a low estimate, and an estimate low by more than that
   fails the run with RDR-242's `ScriptLimitError("result")`, never a short
   page. `nx memory curate`'s 30-day call has to loop on `next_since`.
   RDR-238 leaves this interface's shape to this RDR's gate (its lines
   518-520, `nexus_rdr/240-research-6`); its argument object and row shapes
   are unchanged, and the loop is recorded on RDR-238's bead (nexus-qo7ie)
   as a requirement on `nx memory curate`. A day whose rows alone pass
   768 KB is split into hours, and an hour that still does not fit ends the
   run with a Lua error whose message starts `result-window:` and names the
   hour; it arrives as RDR-242's `ScriptError`, never as a short count. A day
   whose verb result does not fit (A2) fails the run with RDR-242's error
   and is never counted as zero.
6. **`gate_record`.** Covers the review agents' gate records in T2 project
   `<repo>`, the ones the RDR-209 rollup reads. It does not cover the RDR
   gate flow's records in `<repo>_rdr` (`<id>-gate-critique-<date>-r<N>`,
   `<id>-fix-check-<sha>`, `<id>-gate-latest`), which `nx rdr preamble
   rdr-verdict` prints and `nx rdr` reads by those titles
   (`nexus_rdr/240-research-6`); they keep their titles. Arguments `kind`
   (review, critique, confirm, fix-check), `bead`, optional `rdr`, `sha`,
   `outcome` (justified, partial, not-justified), `body`, and the reserved
   `now` and `repo`. Every kind is a title token the audit-tag table already
   matches (`src/nexus/tables/audit-tags.toml`). Returns `{project, title,
   tags, content}`: T2 project `<repo>`, the title
   `<kind>-<sha>-<slug>-<date>` with the date taken from `now`, the tags
   `kind:gate,bead:B[,rdr:N]`, and the record text; the caller writes it with
   `memory_put`, passing the four fields unchanged and no TTL. It refuses an
   outcome outside the verdict vocabulary the review agents already use. The
   agents' T2 write-back line then names `procedure_run("gate_record", ...)`
   followed by that `memory_put`, instead of a free-text tag string, and
   `tests/test_gate_record_tags_lint.py` moves to pin that. Until a write
   verb ships the caller still makes the write, so a caller that edits the
   returned tags is not stopped; the lint and the RDR-209 rollup's own tag
   check are what catch it.
7. **`research_add`.** Arguments `rdr`, `classification`, `method`,
   `finding` and the reserved `repo`. Checks `classification` and `method`
   against a checked table, `src/nexus/tables/rdr-research-vocab.toml`, read
   with `tables.get`; the vocabulary is today two Python constants
   (`_RESEARCH_CLASSIFICATIONS` and `_RESEARCH_METHODS`,
   `src/nexus/commands/rdr.py` lines 2554-2555), and both move to the
   table, which `nx rdr` then reads too. Reads the RDR's existing
   research titles with `memory.list`, takes the next sequence number, and
   returns `{project, title, content}` for `<repo>_rdr/<id>-research-<seq>`
   in the record format the gate
   preamble reads; the caller writes it with `memory_put`, whose content is a
   string and never passes through a shell. The `rdr-research` skill's
   examples change to it, and the bare-argument form of `rdr-research --
   add` prints the replacement and exits nonzero. Two adds to one RDR at the
   same moment can take the same number, and the second write replaces the
   first; the skill tells the caller to read the title back before writing
   and to run the procedure again if it is taken, which narrows that window
   and does not close it.
8. **`preflight`.** Arguments `base` (the commit to diff against); grants
   `git.diff_names` and `git.diff_stat`. Classifies the diff (Python, engine
   `service/`, plugin surface, docs, workflow YAML, schema changeset) from a
   class table held as a local Lua table in the script, reviewed like any
   source line, and returns the checks each class needs as commands: ruff on
   touched files, the class's tripwire tests, the commands that run the
   affected-integration and docs-reader selectors
   (`scripts/select_affected_integration_tests.py`,
   `scripts/select_docs_reader_tests.py`), the drift ledger when the plugin
   surface changed, the gate-jar staleness check when `service/` changed,
   actionlint for workflows. It returns commands and runs none, the
   selectors included: the runtime has no shell, and a heavy check runs
   through `lab-remote` at the caller's hand. A new class is a row in that
   table, not prose. Its classes are nexus's own, so by the Approach's rule
   it lives in the nexus repo's root, `.nexus/procedures/preflight.lua`, and
   is the one procedure here that needs the repo root to run.
9. **Promotion recipe.** A section in the `writing-nx-skills` skill: the
   criterion, the target choice, the citation of sources, and a loss check in
   which `substantive-critic` compares the target with its source files and
   lists every rule the target lacks. A promotion lands only with an empty
   list. The same check applies when the target is a script or a hook.

### Existing Infrastructure Audit

| Need | Exists | Plan |
| --- | --- | --- |
| Sandboxed interpreter | None yet | RDR-242 (nexus-mdbds.1) |
| Transcript instruments | `scripts/skill_routing_census.py`; six ad hoc scans | Fold into `transcripts` |
| Test selection | `scripts/select_affected_integration_tests.py`, `scripts/select_docs_reader_tests.py` | `preflight` returns the commands that run them |
| Verdict vocabulary | `tests/test_review_agent_verdict_vocabulary_lint.py` | `gate_record` refuses an outcome outside it |
| Closed vocabularies | `src/nexus/tables/` | The research vocabulary becomes a table; preflight's classes stay in its script |
| Prose from stdin | `nx memory put -` | Kept; `research_add` covers the verb that lacks it |
| Skill authoring rules | `writing-nx-skills` skill | Add the promotion section |

### Decision Rationale

A fixed sequence becomes a script because a procedure that is read and then
carried out can be misread, and Gap 2's evidence is that it was, with the
rule against it in memory. A script library behind one runner, rather than a
verb per procedure, keeps the MCP surface fixed: one tool, whose description
lists the procedures, instead of a schema per procedure in every context.
Lua in a sandbox, rather than Python in the package, means a repo can add a
procedure without a release, and a procedure can do only what its header
declares. Promotion keeps its sources because the one paper that tests it
found sources and skills together beat skills alone.

## Alternatives Considered

### Alternative 1: A Python verb and MCP tool per procedure

The pre-split RDR-238 design: `nx transcripts`, `nx gate record` with an MCP
twin `gate_record`, `nx preflight`, and stdin on three verbs, chosen by a
front-door rule.

**Pros:** no new runtime; each verb is ordinary, tested Python.

**Cons:** each procedure is a release, a CLI surface and often an MCP schema;
a repo cannot add one without changing nexus.

**Reason for rejection:** Sam's review of RDR-238 (2026-10-31): procedures
become sandboxed Lua scripts.

### Alternative 2: Keep procedures as skills only

**Pros:** no code; skills already load on demand.

**Cons:** a skill is still read and then carried out; the shell split in Gap
2 happened with the rule in memory, and a skill is the same text one level
up.

**Reason for rejection:** a procedure with one right answer is cheaper to
run than to reread. Skills stay the target for procedures that need judgment.

### Briefly Rejected

- **Shell scripts in `scripts/`:** prose still passes through shell parsing,
  and nothing limits what a script can touch.
- **Move every procedure into the conexus plugin:** ships Sam's personal and
  nexus-only rules to every conexus user.

## Trade-offs

### Consequences

- A new runtime dependency, `lupa`, chosen and owned by RDR-242.
- The two record-building procedures do not write; the caller does, until
  a write verb ships with RDR-242's approval document.
- One MCP tool and one CLI verb instead of four of each.
- Agents learn one call shape, `procedure_run(name, args)`, for every
  procedure.

### Risks and Mitigations

- **The runtime is read-only (settled, A1).** `transcripts` and `preflight`
  need only reads, and the two record-building scripts return their record
  for the caller to write. A caller can still alter a returned record before
  writing it; the lint of Technical Design 6 and the read-back of Technical
  Design 7 bound that.
- **A transcript query does not fit RDR-242's limits (A2).** Mitigation:
  the day-at-a-time calls, the per-record cut and the paged result
  (Technical Design 4 and 5); Phase 1 Step 1 measures them before any query
  ships, with a remedy for each miss.
- **A repo procedure nobody reviewed runs.** RDR-242's residual (its
  Technical Design 2, residual S5): an agent that can write to
  `.nexus/procedures/` in a checkout, or a cloned repo that carries one, can
  run a file nobody has reviewed. Version 1 bounds that by the sandbox and
  the read-only verbs. Of the four procedures here only `preflight` lives in
  a repo root, the nexus repo's own, where it is reviewed like any source
  file.
- **Agents keep improvising.** Mitigation: the `procedures` query measures it
  (A3), and the skills and agent write-back lines name the script.
- **A promotion drops a rule.** Mitigation: the loss check (Technical Design
  9) lands nothing with a non-empty list.

### Failure Modes

- Unknown procedure, or a repo procedure shadowing a shipped name: refused,
  error names it.
- A header that breaks the format of Technical Design 1: refused with the
  kind `header`, naming the line.
- Arguments that fail the header's types, or a caller passing a reserved
  `now` or `repo`: refused with the kind `args` before any worker starts.
- A script calls an undeclared verb or exceeds a limit: fails with RDR-242's
  error naming the verb or limit; version 1 verbs only read, so nothing is
  left half done.
- A day's `transcripts.sessions` result over RDR-242's verb-result bound:
  the run fails, and the query reports no count for the window rather than
  a short one. The error is `ScriptLimitError("memory")` once RDR-242 builds
  the table on a protected path (its residual S3, on nexus-mdbds); without
  that the worker aborts and the error is `ScriptWorkerError`.
- A day or hour whose rows pass the 768 KB page bound: a day is split into
  hours, and an hour that still does not fit ends the run with a Lua error
  whose message starts `result-window:` and names it (RDR-242's
  `ScriptError`). A page whose real size passes the 1 MB cap despite the
  estimate fails with `ScriptLimitError("result")`.
- One session's record over 1 MB: cut and marked `truncated`, counted in
  the query's result.
- Two `research_add` runs for one RDR at once: the same sequence number,
  and the second write replaces the first unless the read-back of
  Technical Design 7 catches it.

## Implementation Plan

### Prerequisites

- [x] The Lua runtime RDR accepted: RDR-242, 2026-11-07.
- [ ] RDR-242's read-only runtime landed: `run_script`, the worker, the
      static check and the version 1 verbs (its Phases 1 to 3), with
      `run_script` calling the header parser, which RDR-242's Phase 3 Step 3
      builds to this RDR's format (Technical Design 1), so the parser does
      not wait on this RDR's Phase 1.
- [x] RDR-238 accepted (this RDR does not depend on its implementation).

### Minimum Viable Validation

On a fixture transcript directory: `procedure_run("transcripts", {query =
"tools"})` returns the known per-tool counts, prints no message body, and
the same call through `nx run transcripts --args-file -` returns identical
JSON.

### Phase 1: The runner and the read-only procedures

#### Step 1: Runner, library roots and `transcripts`

`run_procedure`, both front doors, the header and reserved-argument
fixture tests against RDR-242's `run_script`, and the `transcripts` queries with
their paging, each with a fixture test. Then the A2 spike, on the laptop
over its own transcripts for the last 30 days, run through `run_procedure`
so RDR-242's limits apply. It records:

- the wall time of a 30-day `tools` query and of a 30-day `violations`
  query (`bodies=true`), each the total of its paged runs, verb time
  included. Pass: each total under 60 s, A2's "under a minute". RDR-242's
  wall-time limit of 60 s applies to each run, so a total that passes keeps
  every run inside it; the record states the margin.
- the largest one-day `transcripts.sessions` result in the window, in JSON
  bytes, with and without bodies, and the run's peak Lua memory. Pass: the
  largest day at most half of RDR-242's verb-result bound, which its
  residual S3 (on nexus-mdbds) has its Phase 1 Step 2 set from the measured
  JSON-to-Lua ratio (about 22 MB of JSON for this shape,
  `nexus_rdr/240-research-4`; RDR-242's text still says 64 MB), the other
  half left for the query's own state; peak Lua memory under the 64 MB
  `max_memory`.
- the number of runs each 30-day query took, the largest page in JSON
  bytes, and every day split into hours. Pass: every page under the 1 MB
  result cap.
- the number of records cut to 1 MB.

A missed fit line moves the queries to one-hour windows (Technical Design 4
allows timestamps), and the spike runs again. A missed time line, or a fit
line still missed at one hour, comes back to this RDR and to RDR-242
before any query ships, with two of the remedies RDR-242's residual S3
names: a page-by-page verb, or a larger memory limit for a named procedure.
A page line missed (a `result-window:` error or RDR-242's result limit)
comes back the same way. Done when the spike passes and RDR-238's research 2, 4 and 5 can be
re-derived from the queries.

#### Step 2: `preflight`

The class table in the script, the classifier and the selector commands, in
the nexus repo's root. Done when its output for the last ten develop pushes
matches what was run or should have been.

### Phase 2: The record-building procedures

#### Step 1: `research_add`

The checked table `rdr-research-vocab.toml` with both research constants
moved into it, `nx rdr` reading it, and its entry in `tables.get`'s
name-to-file map (RDR-242 Phase 3 Step 1); the script; the `rdr-research`
skill's examples (run the procedure, read the title back, write the record
with `memory_put`); and the bare-argument form's refusal, shipped in the
later package release that Cross-Cutting Concerns orders.

#### Step 2: `gate_record`

The script, the agents' write-back lines naming it and the `memory_put`
that follows, and the lint moved to pin both.

### Phase 3: Promotion

#### Step 1: Recipe

The promotion section in `writing-nx-skills`.

#### Step 2: The four largest files

One at a time, each with its loss check, starting with
`feedback_test_runs_before_push.md`, whose fixed part becomes `preflight` and
whose judgment part, if any remains, becomes a skill.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Procedures | `nx run --list` | the script's header | remove the file | its fixture test | git |

### New Dependencies

None of its own. `lupa>=2.8,<3`, which RDR-242 adds.

## Test Plan

- Header: one fixture per refusal of Technical Design 1 (a missing line, a
  key out of order, an unknown type, a repeated argument name, a
  `procedure` that is not the file name, a summary over 100 characters,
  a non-ASCII byte), each refused with the kind `header` and the line,
  run against RDR-242's parser; a valid header parses
  to its fields; a Lua file whose `grants` line omits a verb it calls is
  refused by RDR-242's `verb-ungranted`, and one that names a verb it never
  calls by `grant-unused`.
- Runner: an unknown name, a shadowing repo name, and mistyped arguments
  (an unknown key, a missing required one, `1.5` for an `integer`) are each
  refused with the kind `args` before any worker starts; a caller passing
  `now` or `repo` is refused; a header declaring them receives the host's
  values (a fixed clock in the test), and one that does not receive neither;
  run from a linked worktree, `repo` is the main checkout's name; an
  undeclared verb call fails naming the verb.
- Front doors: a lint that `procedure_run` and `nx run` call
  `run_procedure` and nothing else.
- Transcripts: each query against a fixture transcript with known counts; a
  test that no query returns a message body unless asked, and that
  `bodies=true` carries assistant text and no tool result; a fixture
  session over 1 MB comes back cut, marked `truncated`, and counted; a
  fixture spanning three days makes three verb calls; a session spanning
  midnight is counted once, on its start day; a fixture whose rows pass
  768 KB returns `next_since`, and the joined pages equal the counts of the
  same fixture counted outside the sandbox in one pass; a page that ends
  mid-day returns a timestamp `next_since`; a single hour over the page
  bound fails with a `result-window:` error.
- Preflight: each change class against a fixture diff; the result contains
  commands and the runner executes none.
- Research add: a finding containing `;`, backticks and `$(...)` returned,
  written with `memory_put` and read back byte for byte; the next sequence
  number follows the fixture RDR's highest; a classification or method
  outside `rdr-research-vocab` is refused, and `nx rdr` reads the same
  table; the bare-argument form exits nonzero.
- Gate record: title (its date from `now`) and tags derived exactly, and
  each kind's title matched by the audit-tag table; an outcome outside the
  vocabulary refused with no record returned.
- Library: a lint that no shipped procedure's `grants` line names a verb
  outside RDR-242's version 1 table (`verb-unknown` refuses it at run time;
  the lint catches it at commit).

## Validation

### Testing Strategy

Runner and procedure tests on fixture directories and a test T2 substrate;
the `procedures` query (A3) read at 30 days after each script lands, stored
in T2.

### Performance Expectations

A2's bounds, measured in Phase 1 Step 1: a 30-day transcript query under
60 s on the laptop across its paged runs, verb time included; the largest
one-day verb result at most half of RDR-242's verb-result bound; every page
under the 1 MB result cap.

## Finalization Gate

Written 2026-11-07, after RDR-242's acceptance, for the first gate.

### Contradiction Check

The draft of 2026-11-01 assumed a write verb (`memory.put`) and a "header
table". RDR-242 as accepted ships no write verb and leaves the header to
this RDR. Every site that carried the old form now carries the new one:
the Approach list, Technical Design 1 to 7, Trade-offs, Failure Modes,
Phase 2, the Test Plan (the `memory.put`-last lint is gone) and New
Dependencies. RDR-238's interface for this RDR (its Technical Design 5:
`{query, since, until, project}`, the four queries and their row shapes)
keeps its argument object and row shapes. Its result is paged, because
RDR-242 caps a run's result at 1 MB: the caller loops on `next_since` and
joins the pages (Technical Design 5). RDR-238 leaves that shape to this
RDR's gate (its lines 518-520), so the paging needs no change to RDR-238's
text; the loop is a requirement on `nx memory curate`, recorded on
RDR-238's bead (nexus-qo7ie).

### Assumption Verification

- A1 is settled by RDR-242's text (`nexus_rdr/240-research-3`): read-only,
  so the fallback design is the design.
- A2 is open, with pass lines and a remedy for each miss (Phase 1 Step 1).
  Its memory half rests on the round-3 critic's measurement of about 2.8
  times JSON size (`nexus_rdr/240-research-4`), which RDR-242's Phase 1
  Step 2 measures again and turns into the bound.
- A3 is measured after each script lands, by the `procedures` query.
- A4 is checked by the loss check, one file at a time, in Phase 3.

#### API Verification

- `run_script(name, args, limits=None)`, its four errors, the version 1
  verb list and its signatures, the name rule and the shadowing refusal are
  quoted from RDR-242 (`nexus_rdr/240-research-3`). None is code yet:
  `src/nexus/lua_runtime/` does not exist (checked 2026-11-07).
- The header form follows the spike's comment-line reader
  (`scripts/spikes/rdr242-lua-sandbox/static_check.py` lines 238-245,
  `nexus_rdr/240-research-5`), changed from two keys (`grants`, `expect`)
  to four and from three lines to four. The parser,
  `nexus.lua_runtime.header.parse`, the `header` and `args` refusal kinds
  and the reserved-argument fill inside `run_script` are requirements on
  RDR-242's Phase 3 Step 3 beyond its Technical Design 4, recorded on
  nexus-mdbds.
- The sandbox has no clock and no verb returns a repo name (RDR-242
  Technical Design 7 and 10, `nexus_rdr/240-research-6`), hence the reserved
  `now` and `repo` arguments.
- `nx memory put -` reads stdin (checked 2026-11-07); `memory_put` takes its
  content as a string.

### Scope Verification

Four procedures, one runner, two front doors, one header format, and a
promotion recipe. Out of scope: any write verb (RDR-242's approval
document), tuple-space verbs (RDR-239 and the aura stage), and whether a
repo root runs by default (left open for Sam by RDR-242's round-3 gate,
residual S5). The RDR gate flow's own records in `<repo>_rdr` are out of
`gate_record`'s scope (Technical Design 6). The bare-argument
refusal of `rdr-research -- add` retires a CLI argument form; it is listed
in Technical Design 7 so the acceptance covers it.

### Cross-Cutting Concerns

- **Security:** RDR-242's sandbox and its read-only verbs; a procedure can
  call only the verbs its header grants, checked against the bytecode.
  Transcript bodies stay out unless asked for, and then only assistant text.
- **Privacy:** query results are counts and names, never bodies.
- **Windows:** the worker and its limits are RDR-242's, per platform; the
  procedures add no platform code.
- **Performance:** A2, measured before any query ships.
- **Upgrade:** no stored record changes format. Research records and the
  RDR gate flow's records keep their titles; the review agents' gate
  records take `gate_record`'s titles from Phase 2 Step 2 on, and older
  ones keep theirs. The `rdr-research` skill ships in the conexus plugin and
  the procedures in the package, which release separately, so the order is:
  a package release adds the procedures and keeps the bare-argument form
  working; the plugin release that follows switches the skill to
  `procedure_run`; a later package release adds the bare-argument refusal.

### Proportionality

The runtime is RDR-242's cost; this RDR adds a header format (whose parser
RDR-242 builds), one thin runner, two thin front doors, one checked table
and four Lua files, against Gap 3's alternative of
four Python verbs and their MCP schemas. The record-building procedures
keep a caller write until a write verb exists, rather than adding one here.

## References

- RDR-238 as filed, commit 740a7a3cd (Gaps 3 and 5, Technical Design 7 to 11,
  Phases 3 and 5)
- Survey artifact: `https://claude.ai/artifact/9gEbyn1wFYhWh9sTJvoT8a`
- T2 `nexus/memory-survey-papers-agent-memory-2026-10-31`,
  `nexus/memory-survey-papers-context-load-2026-10-31`
- Lifelong Context Adaptation through Bidirectional Memory–Skill Learning
  (DEVONthink 4BF492EB)
- Agentic Context Management (DC16C60E)
- SkillsBench (602481F7)
- Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents
  (D0B8FF87)

## Revision History

| Date | Change |
| --- | --- |
| 2026-11-01 | Filed as draft (nexus-qo7ie.2), split from RDR-238 at Sam's review: procedures become sandboxed Lua scripts behind one runner with an MCP and a CLI door. |
| 2026-11-07 | Revised to RDR-242 as accepted: no write verb, so `gate_record` and `research_add` return their record for the caller to write (A1 settled on its fallback); the header is a four-line comment block this RDR owns, with one parser that RDR-242's `run_script` calls (residual S4); queries call `transcripts.sessions` a day at a time, records over 1 MB are cut, bodies are assistant text only, and the A2 spike gains a memory-fit pass line (residual S3); Finalization Gate written. |
| 2026-11-07 | Gate round 1 — BLOCKED (1 Critical, 4 Significant, 1 ship-blocker(s)); commit `9c293bc45`; critique `nexus_rdr/240-gate-critique-2026-11-07-r1`. |
| 2026-11-07 | Round-1 fixes: the transcript result is paged by whole days under RDR-242's 1 MB result cap (`next_since`), with a page line in the A2 spike (C1); the header parser and the `header` and `args` refusal kinds live in RDR-242's `run_script`, built by its Phase 3 Step 3 (S1); reserved `now` and `repo` arguments, `preflight` returns commands and lives in the nexus repo root (S2); `gate_record` covers the review agents' records only, kind `fix-check` (S3); sessions belong to their start window, records carry deferred tools and error flags (S4). |
| 2026-11-07 | Fix-check repairs (fix check `nexus_rdr/240-fix-check-5aea31c79` failed on one row): reserved `repo` is the worktree-stable repo name, and `run_script` fills `now` and `repo` only when the header declares them; `next_since` may be a timestamp and the result echoes `until`; rows are sized by a stated estimate under 768 KB; `result-window:` is a Lua error message, not a kind; join rules for every query; the research vocabulary's plan step. |
| 2026-11-07 | Gate round 2 — PASSED (0 Critical, 6 Significant, 0 ship-blocker(s)); commit `6bc7c1ed1`; critique `nexus_rdr/240-gate-critique-2026-11-07-r2`. |
| 2026-11-07 | Accepted (Sam). Round-2 Significants S1 to S6 dispositioned to nexus-qo7ie (bead comment, with their Implementation Plan steps). |
| 2026-11-08 | Technical Design 4 amended to match the reviewed record of `transcripts.sessions` (nexus-mdbds.8, reviews `nexus/review-nexus-mdbds.8-038adb33b` and `nexus/nexus-mdbds.8-critique-038adb33b`): instruction-load type and size, assistant turns, first-turn context tokens, slash-command names, and list tails dropped and counted past 1 MB. |
| 2026-11-08 | Technical Design 4 amended to match reviewed code (nexus-qo7ie.31; standing grant 3): RDR-242's `transcripts.sessions` takes a fifth argument, `fields` (RDR-242 Technical Design 10), the A2 remedy Sam chose on 2026-11-08. |
