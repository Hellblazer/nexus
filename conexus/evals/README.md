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

15 cases, each named `p<NN>-...` (positive/disambiguation) or
`n<NN>-...` (negative/absorption):

| Case | Target skill | Shape | Grader(s) |
|---|---|---|---|
| p01-using-nx-skills-generic-turn | using-nx-skills | positive | tool_used |
| p02-code-review-before-pr | code-review | positive | tool_used |
| p03-test-authoring-lint-bucket | test-authoring | positive | tool_used |
| p04-rdr-create-schema-decision | rdr-create | positive | tool_used |
| p05-query-rdr-synthesis | query | positive (reduce-from-many) | tool_used |
| p06-orchestration-agent-choice | orchestration | positive | tool_used |
| p07-release-version-bump | release | positive + disambig. vs engine-release | tool_used + llm |
| p08-engine-release-tag-cut | engine-release | positive + disambig. vs release | tool_used + llm |
| p09-debugging-intermittent-npe | debugging | positive + disambig. vs test-validation | tool_used + llm |
| p10-test-validation-coverage-check | test-validation | positive + disambig. vs debugging | tool_used + llm |
| n01-plan-absorption-dinner | strategic-planning / plan-first | negative (absorption) | llm |
| n02-query-absorption-single-fact | query | negative (absorption) | llm |
| n03-release-absorption-release-notes | release / engine-release | negative (absorption) | llm |
| n04-test-authoring-absorption-figurative-test | test-authoring / test-validation | negative (absorption) | llm |
| n05-debugging-absorption-detective-novels | debugging | negative (absorption) | llm |

`release` and `engine-release` (p07, p08, and their absorption/disambig.
counterparts) are **repo-local** skills — they live at
`.claude/skills/release/` and `.claude/skills/engine-release/` in this
checkout, not under `conexus/skills/`, so they ship with this repo's dev
sessions but are not part of the `conexus` plugin distributed via
`marketplace.json`. They're included here because the bead that
commissioned this suite named them explicitly as disambiguation targets
and because a `claude plugin eval` run from this checkout will have them
on the skill list regardless of plugin ownership — but see "Open
questions" below: whether that's the right scope for a suite that ships
*inside* the conexus plugin is unresolved.

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

- **Cost.** ~$0.45/case-run measured ($6.12 and $7.80 for 15 case-runs).
  `--runs 3 --ablation none` is 45 case-runs, so budget **~$20** and set
  the ceiling above it, not at it.

- **Do not set `--threshold` against a moving baseline.** Two of the
  three remaining failures (p07, p08) are structural and can never pass
  in this harness — see Open question 4 — so a threshold tuned to today's
  score encodes those defects as acceptable. Fix the cases, then pick a
  threshold.
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
   weight, arm}`. **`max: 0` is "assert this tool was NOT called"** — all
   nine not-triggered graders (n01–n05, p07–p10) now use it.

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
3. **`case.yaml` context scaffolds.** None of these 15 cases use one —
   every prompt is judged answerable from the plugin's own skill
   descriptions with no repo state dependency. If a real run shows a
   skill firing (or not) differently depending on ambient repo context
   (e.g., whether an RDR directory exists, whether a bead is
   in_progress), add `case.yaml` scaffolds then; nothing here assumes
   their absence is permanent.
4. **Repo-local skill scope.** See the `release`/`engine-release` note
   above — these two skills are not part of the shipped `conexus`
   plugin. A `claude plugin eval` run scoped strictly to the published
   plugin's own surface (as opposed to this dev checkout's full skill
   list) may not have them available at all, in which case p07/p08 and
   n03 would need to move to a repo-local eval location instead of
   `conexus/evals/`, or be dropped from the plugin suite entirely.
5. **`experimental.evals` declaration.** `conexus/.claude-plugin/plugin.json`
   does not currently declare `experimental.evals`. This suite lives at
   the plugin-root convention (`conexus/evals/`) per the format spec,
   which the harness may discover by convention alone — but if explicit
   declaration turns out to be required, add it once the field's real
   shape is confirmed.
