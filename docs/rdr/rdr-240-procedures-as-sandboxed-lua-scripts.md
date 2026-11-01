---
title: "Procedures as Sandboxed Lua Scripts: Run the Fixed Sequence Instead of Recalling It"
id: RDR-240
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-01
accepted_date:
related_issues: [nexus-qo7ie.2, nexus-mdbds.1]
related_rdrs: [RDR-238, RDR-239, RDR-209, RDR-111, RDR-190]
---

# RDR-240: Procedures as Sandboxed Lua Scripts: Run the Fixed Sequence Instead of Recalling It

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Split from RDR-238 on 2026-10-31 at Sam's review: its Gaps 3
and 5 (procedures stored as memory, and procedures recalled instead of run)
move here, and the procedures become sandboxed Lua scripts rather than new
Python verbs or MCP tools. The starting text is RDR-238 as filed at commit
740a7a3cd (Gaps 3 and 5, Technical Design 7 to 11, Phases 3 and 5). This RDR
is the first consumer of the Lua runtime that nexus-mdbds.1 will specify, and
cannot be accepted before that runtime's RDR is.

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
| RDR-238 Memory Curation | Parent; split from it | It owns supersession by deletion, the bounded index and the curation habits. Its curation report's cold-state section takes MCP tool, CLI verb and injected-context counts from this RDR's transcript queries (RDR-238 Technical Design 4); its round-1 gate (2026-11-01) blocked on that dependency, and its fix made this RDR a prerequisite of its Phase 2 and of `nx memory curate`, calling the argument object `{query, since, project}` and the `memory`, `tools`, `loads` and `violations` queries (RDR-238 Technical Design 5). |
| Lua runtime RDR (nexus-mdbds.1, not yet filed) | Depends on it | It owns the sandbox: the interpreter, what a script may call, the limits on time and memory, and how a host verb is exposed. This RDR states what its four procedures need from that runtime and decides nothing about the runtime itself. |
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
  `pyproject.toml` and `uv.lock`, checked 2026-11-01). The runtime RDR picks
  one.
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
`nexus/memory-survey-papers-context-load-2026-10-31`).

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
  writes it with `memory_put`.
- **A2:** Transcript queries written in Lua over host-parsed records are
  fast enough: a 30-day query over about 5,000 sessions finishes in under a
  minute on the laptop. Verification: Phase 1 Step 1's spike, timed.
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
2. `gate_record`: builds a gate record from structured fields, derives its
   title and tags, and writes it, so no agent types a tag.
3. `research_add`: records an RDR research finding from a string, replacing
   the bare-argument form of `nx rdr preamble rdr-research -- add`.
4. `preflight`: reads the diff, names its change classes, and returns the
   checks each class needs, composing the existing selectors.

### Technical Design

1. **Procedure library.** One Lua file per procedure, with a header table
   naming the procedure, its arguments and their types, and the host verbs it
   calls. Two library roots: `src/nexus/procedures/` (ships with `nx`,
   reaches every conexus user) and `.nexus/procedures/` in a repo (that
   repo's own procedures). A repo procedure may not shadow a shipped one; the
   runner refuses the name.
2. **Runner.** One Python function, `run_procedure(name, args)`, loads the
   script into the runtime of nexus-mdbds.1, checks `args` against the
   header's types, exposes only the host verbs the header names, and returns
   the script's result as JSON. A script that calls an undeclared verb, or
   one that runs past the runtime's limits, fails with an error naming the
   verb or the limit; nothing it wrote before the failure is rolled back, so
   a write verb is the script's last call by convention, and the lint in
   Test Plan checks it.
3. **Front doors.** The MCP tool `procedure_run` and the CLI verb `nx run`
   both call `run_procedure` and nothing else. `nx run --list` and the MCP
   tool's description list the library's procedures with one line each.
4. **Host verbs the four procedures need** (requirements on the runtime RDR,
   which decides their shape):
   - `transcripts.sessions(since, project)`: yields one record per session
     or subagent transcript, parsed in Python, with tool calls, instruction
     loads, model, token counts and sizes. Never message bodies unless the
     caller passes `bodies=true`, since transcripts hold everything a
     session saw.
   - `memory.get`, `memory.list`, `memory.search`: the T2 reads.
   - `memory.put`: the one write verb, granted only to a script whose header
     declares it.
   - `git.diff_names(base)` and `git.diff_stat(base)`: read-only, in the
     caller's working tree.
   - `tables.get(name)`: a checked table from `src/nexus/tables/`.
5. **`transcripts`.** Queries, each a function in the script with a fixture
   test: `agents` (dispatches by agent type and model, starting context,
   turns, errors, estimated cost), `tools` (calls per tool and MCP server,
   deferred against called, and per `nx` CLI verb called through Bash, which
   RDR-238's cold-state report needs), `skills` (the skill-routing census, ported),
   `memory` (memory-file reads and writes, T2 reads by project and title
   from `memory_get` and `memory_search` calls, and what loaded into a subagent's
   instructions), `violations` (named rule detectors, counted per 100 main
   sessions and reading message bodies inside the procedure, RDR-238's Phase
   2 before and after counts), `loads` (instruction files, their types and
   sizes, per main session and per dispatch), and
   `procedures` (script runs against hand-written equivalents, A3). Cost
   uses one dated price table and says it is an estimate.
6. **`gate_record`.** Arguments `kind` (review, critique, confirm,
   fixround), `bead`, optional `rdr`, `sha`, `outcome` (justified, partial,
   not-justified) and `body`. Writes T2 project `<repo>` with the title
   `<kind>-<sha>-<slug>-<date>`, tags `kind:gate,bead:B[,rdr:N]`, and no TTL,
   and refuses an outcome outside the verdict vocabulary the review agents
   already use. The agents' T2 write-back line then names
   `procedure_run("gate_record", ...)` instead of a free-text tag string, and
   `tests/test_gate_record_tags_lint.py` moves to pin that.
7. **`research_add`.** Arguments `rdr`, `classification`, `method` and
   `finding`; assigns the next sequence number and writes
   `<repo>_rdr/<id>-research-<seq>` in the record format the gate preamble
   reads. The `rdr-research` skill's examples change to it, and the
   bare-argument form of `rdr-research -- add` prints the replacement and
   exits nonzero.
8. **`preflight`.** Classifies the diff (Python, engine `service/`, plugin
   surface, docs, workflow YAML, schema changeset) from a checked table,
   `src/nexus/tables/preflight_classes`, and returns the checks each class
   needs: ruff on touched files, the class's tripwire tests, the
   affected-integration and docs-reader selectors' output, the drift ledger
   when the plugin surface changed, the gate-jar staleness check when
   `service/` changed, actionlint for workflows. It returns commands and
   runs none: the runtime has no shell, and a heavy check runs through
   `lab-remote` at the caller's hand. A new class is a table row, not prose.
9. **Promotion recipe.** A section in the `writing-nx-skills` skill: the
   criterion, the target choice, the citation of sources, and a loss check in
   which `substantive-critic` compares the target with its source files and
   lists every rule the target lacks. A promotion lands only with an empty
   list. The same check applies when the target is a script or a hook.

### Existing Infrastructure Audit

| Need | Exists | Plan |
| --- | --- | --- |
| Sandboxed interpreter | None | nexus-mdbds.1 |
| Transcript instruments | `scripts/skill_routing_census.py`; six ad hoc scans | Fold into `transcripts` |
| Test selection | `scripts/select_affected_integration_tests.py`, `scripts/select_docs_reader_tests.py` | `preflight` returns their output |
| Verdict vocabulary | `tests/test_review_agent_verdict_vocabulary_lint.py` | `gate_record` enforces it at write |
| Closed vocabularies | `src/nexus/tables/` | Preflight's change classes become a table |
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

- A new runtime dependency, chosen and owned by nexus-mdbds.1.
- One MCP tool and one CLI verb instead of four of each.
- Agents learn one call shape, `procedure_run(name, args)`, for every
  procedure.

### Risks and Mitigations

- **The runtime RDR stalls or lands read-only.** Mitigation: A1's fallback;
  `transcripts` and `preflight` need only reads, and the two writing scripts
  return their record for the caller to write.
- **Agents keep improvising.** Mitigation: the `procedures` query measures it
  (A3), and the skills and agent write-back lines name the script.
- **A promotion drops a rule.** Mitigation: the loss check (Technical Design
  9) lands nothing with a non-empty list.

### Failure Modes

- Unknown procedure, or a repo procedure shadowing a shipped name: refused,
  error names it.
- Arguments that fail the header's types: refused before the script runs.
- A script calls an undeclared verb or exceeds a limit: fails, error names
  the verb or limit, earlier reads have no effect.

## Implementation Plan

### Prerequisites

- [ ] The Lua runtime RDR (nexus-mdbds.1) accepted, and its read-only runtime
      landed.
- [ ] RDR-238 accepted (this RDR does not depend on its implementation).

### Minimum Viable Validation

On a fixture transcript directory: `procedure_run("transcripts", {query =
"tools"})` returns the known per-tool counts, prints no message body, and
the same call through `nx run transcripts --args-file -` returns identical
JSON.

### Phase 1: The runner and the read-only procedures

#### Step 1: Runner, library roots and `transcripts`

`run_procedure`, both front doors, the header check, and the `transcripts`
queries, each with a fixture test. Time a 30-day query on the laptop (A2).
Done when RDR-238's research 2, 4 and 5 can be re-derived from it.

#### Step 2: `preflight`

The change-class table, the classifier and the composed selectors. Done when
its output for the last ten develop pushes matches what was run or should
have been.

### Phase 2: The writing procedures

#### Step 1: `research_add`

The script, the `rdr-research` skill's examples, and the bare-argument form's
refusal.

#### Step 2: `gate_record`

The script, the agents' write-back lines naming it, and the lint moved to pin
it.

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

A Lua interpreter, chosen by nexus-mdbds.1.

## Test Plan

- Runner: an unknown name, a shadowing repo name, and mistyped arguments are
  each refused before the script runs; an undeclared verb call fails naming
  the verb.
- Front doors: a lint that `procedure_run` and `nx run` call
  `run_procedure` and nothing else.
- Transcripts: each query against a fixture transcript with known counts; a
  test that no query returns a message body unless asked.
- Preflight: each change class against a fixture diff; the result contains
  commands and the runner executes none.
- Research add: a finding containing `;`, backticks and `$(...)` stored byte
  for byte; the bare-argument form exits nonzero.
- Gate record: title and tags derived exactly; an outcome outside the
  vocabulary refused with nothing written.
- Library: a lint that every script granted `memory.put` calls it last.

## Validation

### Testing Strategy

Runner and procedure tests on fixture directories and a test T2 substrate;
the `procedures` query (A3) read at 30 days after each script lands, stored
in T2.

### Performance Expectations

A2's bound: a 30-day transcript query under a minute on the laptop.

## Finalization Gate

Not yet written. The gate waits on the Lua runtime RDR (nexus-mdbds.1).

### Contradiction Check

### Assumption Verification

#### API Verification

### Scope Verification

### Cross-Cutting Concerns

### Proportionality

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
