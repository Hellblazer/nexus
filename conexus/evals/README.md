# Skill-triggering eval suite (nexus-7zup9)

`claude plugin eval` corpus for the conexus plugin's skill-triggering
surface: does the right skill fire on the right prompt, and — the defect
class we've actually paid for (`nexus-77cct`, the retired plan-meta
skills that "absorb any question containing the word 'plan'") — does the
wrong skill stay silent on a prompt that only superficially resembles its
trigger phrasing.

## Status: runnable, three runs executed

`claude plugin eval` is early access and gated: every invocation path
prints "`plugin eval` is currently in early access" and exits 1 (`--help`
renders anyway, because commander prints help before the gate). The
binary documents its own bypass — set `CLAUDE_CODE_WALNUT_SPIRE=1` in the
shell env or in `~/.claude/settings.json`'s `env` block (NOT the repo's
`.claude/settings.json`). With it set the runner works end to end here.

The earlier "not enabled on this box" was accurate; the implied
"therefore unrunnable" was not, and it cost this corpus two release
cycles of being repaired against documentation instead of against a run.

Full runs to date (the first three `--runs 1 --ablation none` on the 15-case corpus):

| Corpus state | Passed | Score | Cost |
|---|---|---|---|
| v7.16.0 | 11/15 | 0.833 | $6.69 |
| v7.16.1 (nine graders unsatisfiable) | 4/15 | 0.333 | $7.80 |
| nexus-dkotg (`min: 0` added) | 12/15 | 0.867 | $6.12 |
| nexus-dg7i6.7 (outcome-graded, 16 cases, `--runs 3 --scaffold`) | 13/16 | 0.955 | $19.59 |

The 2026-10-17 run (claude 2.1.300, claude-opus-5-5, chas docker,
subscription OAuth, so the dollar figure is the runner's notional
`costUsd`, not spend) passed p04, p06, p11 to p14, o01, o02, o05 and the
four n-cases 3/3. Below 1.0: a01 2/3 (one run dispatched no reviewer),
o03 2/3 on the llm ranking criterion, and o04 1/3 on the verdict regex
while its llm grader passed 3/3; a rerun of o04 with `--keep-temp`
passed 3/3 and showed the answers, so no grader defect was found. One
caveat on that run: inside docker, Claude Code's Bash sandbox could start
bubblewrap but not apply its seccomp filter, so every shell command
failed and the model worked by reading files. Read the run as a
shell-less baseline. A second run on 2026-10-17 in a `--privileged`
container scored 14/16 (0.953) and was shell-less too: its kept traces
show every Bash call failing with `apply-seccomp: write
/proc/self/setgroups (nested userns is capability-restricted; caller
must provide CAP_SYS_ADMIN)`. Claude Code's seccomp step creates a user
namespace inside bubblewrap's, and chas (Ubuntu 26.04) sets
`kernel.apparmor_restrict_unprivileged_userns = 1`, which refuses the
nested namespace to every process in the container, root included
(probe: `bwrap --unshare-user ... bwrap --unshare-user ... /bin/true`
fails, a single level succeeds). No container flag fixes that; a run
with a shell needs that sysctl relaxed on the host, or a host whose
sandbox is not bubblewrap (macOS).

## Layout

```
conexus/evals/
  README.md                    this file
  <case-name>/
    prompt.md                  YAML frontmatter (name:) + the user prompt
    case.yaml                  outcome cases only: schema_version "1.1", name,
                               context.scaffold_script: fixture.sh
    fixture.sh                 outcome cases only: builds the project the
                               prompt talks about, in an empty directory
    graders/
      <grader-name>.md          YAML frontmatter (type: + type-specific fields)
```

16 cases. The letter says how the case is graded:

- `p<NN>`: a procedural skill must load (`tool_used` on `Skill`).
- `a<NN>`: an agent must be dispatched (`tool_used` on `Agent`, `min` 1).
- `o<NN>`: the outcome is graded (`regex` on the final message or a file,
  `llm` criteria), on a project the case's scaffold builds.
- `n<NN>`: absorption guard, a skill or agent must stay silent
  (`tool_used`, `min: 0, max: 0`).

| Case | Graded on | Graders |
|---|---|---|
| p04-rdr-create-schema-decision | `rdr-create` loads | tool_used |
| p06-orchestration-agent-choice | `orchestration` loads | tool_used |
| p11-rdr-gate-complete-draft | `rdr-gate` (command) or `rdr-gate-checklist` loads on a complete draft RDR | tool_used |
| p12-rdr-close-shipped | `rdr-close` loads for an accepted, shipped RDR | tool_used |
| p13-continuation-handoff | `continuation` loads before a `/clear` | tool_used |
| p14-phase-review-gate | `phase-review-gate` loads at a phase boundary | tool_used |
| a01-review-gate-dispatch | the quality gate dispatches `code-review-expert` and `substantive-critic`, with "Ship when" in the brief | tool_used ×3 |
| o01-voyage-retry-wrapper | the Voyage call gains a bounded retry on HTTP errors, in `src/embed.py` or a helper module; only transient failures retried | regex ×2 (trace, anchored to a Write/Edit input to a non-test file), llm (file) |
| o02-review-branch-before-pr | the review names the three planted defects: logged API key, `os.system` injection, world-writable cache | regex ×3, llm |
| o03-rdr-synthesis-ranking | all five RDRs covered; a ranked top three | regex, llm |
| o04-retry-coverage-verdict | "not enough", naming the missing retry tests; debugger agent not dispatched | regex, llm, tool_used `0..0` |
| o05-intermittent-npe-race | the race in `ConnectionPool` named as the root cause; test-validator agent not dispatched | regex, llm, tool_used `0..0` |
| n01-plan-absorption-dinner | strategic-planning / plan-first / architecture skills and the strategic-planner agent silent | tool_used `0..0` ×2 |
| n02-query-absorption-single-fact | `nx_answer` not called for a one-symbol lookup | tool_used `0..0` |
| n04-test-authoring-absorption-figurative-test | test-validator agent silent | tool_used `0..0` |
| n05-debugging-absorption-detective-novels | debugger agent silent | tool_used `0..0` |

o01's regexes read the trace, so a retry counts in whichever non-test file the
model writes it to (nexus-pztjj). Its judge reads only the final `src/embed.py`:
`focus: trace` gives the judge the first and last messages of a run, and the live
run's middle, retry edit included, was elided. A retry body in a helper module
is therefore token-checked, never judged for the transient-only rule. That is
an accepted limit (nexus-ejdlu, closed on Sam's call): a probe on chas with
Claude Code 2.1.300 showed no grader source reaches a new file's contents.
The judge's `focus: files` receives only the new paths, `{source: file}`
takes one exact path and refuses a glob, and `focus: trace` keeps the first
and last 12 messages. In the 6 live o01 runs the model never wrote a helper.
The regexes grade what the model attempted, not the file it ended with: a
Write or Edit that failed, or a retry a later edit removed, still counts, and
a write made through Bash is not seen. `files` targets carry paths only, so
the trace is the only grader source that spans several files.

### The grading rule (nexus-dg7i6.7, 2026-10-17)

A case asserts that a skill loaded only when the skill carries a procedure
or format the model cannot produce without it (`rdr-create`'s template,
`orchestration`'s agent table). A task the model rightly does itself is
graded on the result. This follows nexus-dg7i6.5: four of the five
former p-cases (`using-nx-skills`, `code-review`, `query`,
`test-validation`) loaded their skill 0 times in 12 eval runs even with
the premise true, while the model did the task well. The fifth,
`debugging`, did load (3/3 on the plain project, 2/3 with a true premise);
it was retired on the real-use evidence alone: a 30-day census of 509
sessions had 0 model loads of all six retired skills against 602
`code-review-expert`, 153 `developer` and 27 `debugger` dispatches
(nexus-dg7i6.6). The cases became o01 to o05. The procedural skills that
do load in real use get p-cases (p11 to p14; census loads: rdr-gate 25,
continuation 15, phase-review-gate 11, rdr-close 7), and the review gate
that replaced the `code-review` skill gets a01.

An absorption guard names a surface that still exists: the agent, or the
`nx_answer` tool, never a retired skill, since `max: 0` on a name that
cannot be called passes vacuously. The guards are regression guards with
no observed base rate: no 2026-10-17 run dispatched any agent, so their
passes show the surface stayed silent, not that the guard could fire.
a01 is the positive control for the mechanism: it asserts an `Agent`
dispatch with a `subagent_type`, the same encoding the guards assert
absent.

Outcome cases need `--scaffold` (below): without it the workspace is
empty and every outcome grader fails. Each `fixture.sh` writes fixed
content with fixed commit dates and no network, and
`tests/test_eval_corpus_lint.py` builds each one twice and requires the
same commit. Before a grader is trusted it must pass on answers where the
defect or fact is present and fail on wrong or clearing ones. Each regex
grader has crafted must-pass and must-fail answers pinned in
`test_regex_grader_controls` (the fail lists include the review critique's
counterexamples: "no secrets are logged", "stack trace", "no reason to hold
the PR, the retry path is solid"). On real answers, the skillroute harness
runs of 2026-10-17 passed every o-case regex grader with the fixture
present; the plain-project runs failed them, which shows only that an
answer about nothing fails. The `llm` graders and the new schema shapes
(`target: { source: file, path }`, `focus`, `case.yaml` scaffolds) were
checked against the documentation and the harness's own scorer, not yet
against `claude plugin eval` itself; the first scaffolded run does that.

### Dropped: n03, p07, p08 (nexus-dkotg, 2026-08-23)

These three targeted `release` and `engine-release`, which are
**repo-local** skills at `.claude/skills/` in this checkout, not under
`conexus/skills/` — so they are not part of the plugin distributed via
`marketplace.json`. The eval sandbox runs in a temp HOME and never loaded
them: p07 and p08 reported `Skill called 0x` in every run of all three
full runs, and n03 passed **vacuously**, its claim never actually tested.

A $2.43 probe settled whether relocation could rescue them. It cannot:
targeting `.claude` resolves a *plugin*, not the repo-local skills
directory, and `release-triggered` still reported `0x` across three runs.

Dropping them also retires a claim that would not have survived being
tested. n03 asserted `release` must stay silent on "help me write the
release notes" — but that skill's own checklist has a literal step
**"### 4. Update both changelogs"**, so the case demanded silence from a
skill whose documented job covers the request. The vacuous pass was
hiding a contestable premise.

**Repo-local skill triggering is now an explicit non-goal of this
corpus.** A suite that ships inside the conexus plugin tests the conexus
plugin's own surface. If `release`/`engine-release` coverage is wanted,
it belongs in a separate repo-local suite with its own invocation.

### Dropped: p03 (nexus-cnzei.6, 2026-09-13)

Same reason as n03/p07/p08 above, one skill later: `test-authoring` moved
to `.claude/skills/test-authoring/` — it was nexus-repo-specific content
(this repo's own test suite, dev-loop layers, `tests/AGENTS.md`) shipping
to every conexus plugin user for no benefit outside this repo. p03's
positive `tool_used` grader on `conexus:test-authoring` would now fail the
same way p07/p08 did (the sandbox never loads a repo-local skill), so it
was deleted rather than left to discover that the hard way. n04's negative
grader kept its `test-validation` half; the `test-authoring` alternative
in its regex is gone too, for the same reason.

### Hazard: worktrees pollute discovery

Point the runner at a directory containing git worktrees and it walks
into them. The probe above, targeting `.claude`, resolved its plugin from
`.claude/worktrees/skeets-session-parallel/conexus` and ran the same case
**twice** — once from the intended corpus and once from the worktree's
stale copy, which was on `develop` and still carried the pre-fix
`expected 1..0` graders.

Target `conexus/` specifically, never a parent that contains
`.claude/worktrees/`. Verify by checking `suite.root` and the case count
in the JSON report: this corpus is 16 cases with 16 unique names. It is
read-only — nothing was written into any worktree — but a run scored
against a stale corpus is a wrong answer delivered confidently.

## Running

```bash
CLAUDE_CODE_WALNUT_SPIRE=1 claude plugin eval \
  --runs 3 --ablation none --scaffold --trust-plugin --no-publish \
  --mocks off --allow-tools Bash Write Edit mcp__plugin_conexus_nexus \
    mcp__plugin_conexus_nexus-catalog mcp__plugin_conexus_sequential-thinking \
  --max-cost-usd 100 --json report.json <path-to-conexus-plugin>
```

- **Opt the run out of the install ping.** `claude plugin eval` hands a
  plugin MCP server only the env its `.mcp.json` names, and every run gets
  a fresh HOME, so each run of an installed `nx` counts as a new install.
  Add `"env": {"NX_NO_TELEMETRY": "1"}` to each server in the copy you
  evaluate, as `pe_in_container.sh` does (nexus-ca5rw); exporting the
  variable in your shell does not reach the server.
- **Tool grants are part of the measurement.** Without `--allow-tools`
  the runner removes Bash, Write and Edit, so o01 cannot edit its file
  and o02/o04 cannot read git or run the tests. Without `--mocks off` the
  plugin's MCP servers do not start, so the n02 `nx_answer` guard passes
  vacuously. `Agent` needs no grant (a01 dispatched both reviewers with
  none). `mcp__*` is refused; name each server.
- **Granting Bash needs a sandbox backend** (bubblewrap and socat on
  Linux); without one the runner refuses every case. In docker that also
  needs `--security-opt seccomp=unconfined --security-opt
  apparmor=unconfined --security-opt systempaths=unconfined`:
  `tests/e2e/prefix-capture/ab/skillroute/pe_detach.sh` runs it that way
  on chas. Those flags let the sandbox start but not run commands on a
  host that restricts unprivileged user namespaces (see the run notes
  above); check a trace for `apply-seccomp` errors before reading a
  score as a run with a shell.
- **`--no-publish`** keeps the HTML report local; the default publishes
  it to claude.ai.
- **Case filters match the `name:` field**, not the directory
  (`--case 'review-gate*'`, not `'a01*'`).

- **`--runs 3` is the floor, not a luxury.** At `--runs 1` the positive
  half of this corpus is not a measurement. Measured across the three
  runs above, a DIFFERENT positive case flipped verdict every single time
  while nothing about it changed:

  | Case | run 1 | run 2 | run 3 |
  |---|---|---|---|
  | p01 using-nx-skills-generic-turn | pass | fail | pass |
  | p05 query-rdr-synthesis | pass | fail | pass |
  | p02 code-review-before-pr | pass | pass | fail |

  Skill triggering is a model decision, so it is nondeterministic, and a
  single sample cannot separate "this skill stopped firing" from "this
  run happened to go the other way". The negative half does not flap —
  all nine `min: 0, max: 0` graders were unanimous across every run — so
  the noise is specific to asserting that something DID fire. Any number
  read off a `--runs 1` invocation is a coin, including the 12/15 above.

- **Do not run the bare defaults.** `--runs` defaults to `case.runs ?? 3`
  and `--ablation` defaults to `with-without`, which is 90 case-runs at
  roughly **$67**. Passing `--max-cost-usd 10` with the defaults, as this
  file used to recommend, aborts at about 15% with exit 2 and reports a
  partial as though it were a run.

- **Cost.** ~$0.45/case-run was measured on the old one-turn
  skill-trigger cases ($6.12 and $7.80 for 15 case-runs). The outcome and
  agent cases are multi-turn agentic runs (30 to 60 s each on the
  skillroute harness) plus llm judge votes, so that rate does not carry
  over. Measured 2026-10-17: one a01 run (two reviewer dispatches) cost
  $1.45 and took 198 s; the first full scaffolded `--runs 3` run (48
  case-runs) sets the corpus figure. Set the ceiling well above the estimate, not at it: a ceiling
  hit aborts with a partial report.

- **Do not set `--threshold` yet.** The two structural failures (p07,
  p08) are gone, but no `--runs 3` baseline exists — every number this
  corpus has produced came from a single sample, and the flap table above
  shows what that is worth on the positive half. Establish a `--runs 3`
  baseline first, then pick a threshold against it.
- Exit code contract (per the CI format spec): `0` = pass, `1` =
  fail/no cases, `2` = partial. Wire the CI job on that exit code, not on
  parsing `report.json`'s pass/fail language, since the aggregate-result
  schema version could shift under a "v1" label change.

In CI, this suite gates PRs that touch `conexus/skills/*/SKILL.md`,
`conexus/agents/*.md`, or `.claude-plugin/plugin.json`'s
`experimental.evals` — the surfaces that can silently move which skill a
given prompt triggers. It does not need to run on every PR; skill
descriptions change rarely relative to code.

## Maintenance rule (future lint hook)

**Every new skill ships with at least one positive case and, if its
description or name shares a common word with another skill's trigger
phrasing (the `nexus-77cct` "plan" class), at least one negative case.**
This line is written to become a mechanized check later — a script that
diffs `conexus/skills/*/SKILL.md` against `conexus/evals/*/prompt.md`
target coverage and fails a PR that adds a skill with zero eval cases.
No such script exists yet; this paragraph is the spec for whoever writes
it.

## Open questions for enablement day

These are the concrete unknowns this suite carries because it was
authored against the documented format only, never against a live run:

1. **Skill-name form in `input_match`.** The available-skills listing
   this session sees shows conexus-plugin skills with a `conexus:`
   prefix (`conexus:code-review`, `conexus:debugging`, ...) and
   repo-local skills bare (`release`, `engine-release`). Every
   `tool_used` grader here matches `"skill"\s*:\s*"(conexus:)?<name>"`
   for plugin skills and the bare form only for repo-local skills — but
   this is inferred from the listing format, not confirmed against an
   actual `Skill` tool-call payload. Check the first real
   `report.json`'s per-case transcript for the literal `skill` field
   value and tighten or loosen the regex accordingly.
2. **RESOLVED 2026-08-23 — there IS a negation primitive, and the `llm`
   graders were blind.** This entry used to say no primitive existed, and
   that error is what produced the defect it now records.

   `tool_used` takes `min` and `max`. Read from the binary's own schema:
   `{type:"tool_used", name, tool, input_match?, min:int>=0?, max:int>=0?,
   weight, arm}`. **`max: 0` is "assert this tool was NOT called" — and it
   requires `min: 0` alongside it, because `min` DEFAULTS TO 1.** Setting
   `max: 0` alone yields `expected 1..0`, which nothing can satisfy; that
   was nexus-dkotg, and it silently voided every not-triggered grader in
   7.16.1. `test_grader_bounds_are_satisfiable` now refuses that shape at
   PR time. Nine graders were converted here; six survive the n03/p07/p08
   drop (n01, n02, n04, n05, and the negative halves of p09 and p10).

   The `llm` graders they replace could not do the job they were written
   for. Every one began "Fail if the transcript contains a Skill tool
   call…", but an `llm` grader's `focus` field defaults to
   `last_message` (`focus: c2h().default("last_message")`, where `c2h` is
   `enum(["trace","last_message","files"])`), so the judge received only
   `run.lastAssistantText` and never saw a tool call at all. Only
   `focus: trace` passes tool calls. Proven in a real run before the
   rewrite: an `llm` grader voted FAIL 3/3 on a trace where a paired
   `tool_used max: 0` counter on the same trace reported the skill was
   never invoked — unanimously wrong against ground truth.

   (`criteria` *was* the right field name for `llm`; that half of the
   guess was correct.)
3. **RESOLVED 2026-10-17 — `case.yaml` context scaffolds are in use.**
   The o-cases, a01 and p11 to p14 build their project with
   `context.scaffold_script: fixture.sh`, which runs only under
   `--scaffold`. nexus-dg7i6.5 showed why: on a project where the
   prompt's premise was false, the model checked the repo and answered
   without the skill, so repo state was part of what the case measured.
4. **RESOLVED 2026-08-23 — repo-local skills are out of scope, and the
   cases are dropped.** This was filed as a hypothetical and was already
   a live defect: the sandbox never loaded `.claude/skills/`, so p07 and
   p08 could not pass and n03 passed vacuously, across three full runs.
   Relocation was probed and does not work. n03, p07 and p08 are removed;
   see "Dropped" above. Repo-local skill triggering is an explicit
   non-goal of this corpus.

   The `debugging`/`debug` and `query`/`plan-first` disambiguation item
   that used to follow here is moot: `debugging` and `query` were retired
   by nexus-dg7i6.6, and the `debug` command is hidden from the model.
5. **`experimental.evals` declaration.** `conexus/.claude-plugin/plugin.json`
   does not currently declare `experimental.evals`. This suite lives at
   the plugin-root convention (`conexus/evals/`) per the format spec,
   which the harness may discover by convention alone — but if explicit
   declaration turns out to be required, add it once the field's real
   shape is confirmed.
