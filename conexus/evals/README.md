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

Three full runs to date, all `--runs 1 --ablation none`, 15 cases:

| Corpus state | Passed | Score | Cost |
|---|---|---|---|
| v7.16.0 | 11/15 | 0.833 | $6.69 |
| v7.16.1 (nine graders unsatisfiable) | 4/15 | 0.333 | $7.80 |
| nexus-dkotg (`min: 0` added) | 12/15 | 0.867 | $6.12 |

## Layout

```
conexus/evals/
  README.md                    this file
  <case-name>/
    prompt.md                  YAML frontmatter (name:) + the user prompt
    graders/
      <grader-name>.md          YAML frontmatter (type: + type-specific fields)
```

12 cases, each named `p<NN>-...` (positive/disambiguation) or
`n<NN>-...` (negative/absorption). Numbering is historical and has gaps —
n03, p07 and p08 were dropped, see below.

| Case | Target skill | Shape | Graders |
|---|---|---|---|
| p01-using-nx-skills-generic-turn | using-nx-skills | positive | tool_used |
| p02-code-review-before-pr | code-review | positive | tool_used |
| p03-test-authoring-lint-bucket | test-authoring | positive | tool_used |
| p04-rdr-create-schema-decision | rdr-create | positive | tool_used |
| p05-query-rdr-synthesis | query | positive (reduce-from-many) | tool_used |
| p06-orchestration-agent-choice | orchestration | positive | tool_used |
| p09-debugging-intermittent-npe | debugging | positive + disambig. vs test-validation | tool_used ×2 |
| p10-test-validation-coverage-check | test-validation | positive + disambig. vs debugging | tool_used ×2 |
| n01-plan-absorption-dinner | strategic-planning / plan-first / architecture | negative (absorption) | tool_used `0..0` |
| n02-query-absorption-single-fact | query | negative (absorption) | tool_used `0..0` |
| n04-test-authoring-absorption-figurative-test | test-authoring / test-validation | negative (absorption) | tool_used `0..0` |
| n05-debugging-absorption-detective-novels | debugging / debug | negative (absorption) | tool_used `0..0` |

Every grader is `tool_used`. There are no `llm` graders left — they could
not see tool calls (their `focus` defaults to `last_message`), which is
what nexus-7zup9 removed.

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

### Hazard: worktrees pollute discovery

Point the runner at a directory containing git worktrees and it walks
into them. The probe above, targeting `.claude`, resolved its plugin from
`.claude/worktrees/skeets-session-parallel/conexus` and ran the same case
**twice** — once from the intended corpus and once from the worktree's
stale copy, which was on `develop` and still carried the pre-fix
`expected 1..0` graders.

Target `conexus/` specifically, never a parent that contains
`.claude/worktrees/`. Verify by checking `suite.root` and the case count
in the JSON report: this corpus is 12 cases with 12 unique names. It is
read-only — nothing was written into any worktree — but a run scored
against a stale corpus is a wrong answer delivered confidently.

## Running

```bash
CLAUDE_CODE_WALNUT_SPIRE=1 claude plugin eval \
  --runs 3 --ablation none --max-cost-usd 30 \
  --json report.json <path-to-conexus-plugin>
```

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

- **Cost.** ~$0.45/case-run measured ($6.12 and $7.80 for 15 case-runs,
  when the corpus was 15). At 12 cases, `--runs 3 --ablation none` is 36
  case-runs, so budget **~$16** and set the ceiling above it, not at it.

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
3. **`case.yaml` context scaffolds.** None of these 12 cases use one —
   every prompt is judged answerable from the plugin's own skill
   descriptions with no repo state dependency. If a real run shows a
   skill firing (or not) differently depending on ambient repo context
   (e.g., whether an RDR directory exists, whether a bead is
   in_progress), add `case.yaml` scaffolds then; nothing here assumes
   their absence is permanent.
4. **RESOLVED 2026-08-23 — repo-local skills are out of scope, and the
   cases are dropped.** This was filed as a hypothetical and was already
   a live defect: the sandbox never loaded `.claude/skills/`, so p07 and
   p08 could not pass and n03 passed vacuously, across three full runs.
   Relocation was probed and does not work. n03, p07 and p08 are removed;
   see "Dropped" above. Repo-local skill triggering is an explicit
   non-goal of this corpus.

   The open item that survives: `debugging` and `debug` both exist under
   `conexus/skills/` with overlapping descriptions ("tests fail,
   exceptions occur" vs "debugging a failing code path"). n05 and p10's
   negatives now match both. p09 has NO grader asserting `debug` stays
   silent while `debugging` fires — that is a claim about where the
   boundary sits, and it should be settled by watching a run rather than
   asserted into the corpus. Same for `query` vs `plan-first`, whose
   descriptions both say the answer "must be reduced from many
   documents"; p05 covers `query` positively but nothing disambiguates
   the pair.
5. **`experimental.evals` declaration.** `conexus/.claude-plugin/plugin.json`
   does not currently declare `experimental.evals`. This suite lives at
   the plugin-root convention (`conexus/evals/`) per the format spec,
   which the harness may discover by convention alone — but if explicit
   declaration turns out to be required, add it once the field's real
   shape is confirmed.
