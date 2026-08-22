# Skill-triggering eval suite (nexus-7zup9)

`claude plugin eval` corpus for the conexus plugin's skill-triggering
surface: does the right skill fire on the right prompt, and — the defect
class we've actually paid for (`nexus-77cct`, the retired plan-meta
skills that "absorb any question containing the word 'plan'") — does the
wrong skill stay silent on a prompt that only superficially resembles its
trigger phrasing.

## Status: authored, not runnable here

`claude plugin eval` is early access and not enabled on this box
(`claude plugin eval` prints "currently in early access", exit 1, as of
this writing). This suite was authored blind to actual runtime behavior —
no case here has ever executed. Enablement is gated on an org-level
setting from Anthropic; ask your Anthropic contact to turn it on for this
org before attempting a run. Everything under "Open questions" below is a
concrete verification task for whoever runs this suite first.

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

## Running (once enabled)

```bash
claude plugin eval --json report.json --max-cost-usd 10 --threshold 0.8
```

- `--max-cost-usd 10`: each case is a real model run through a full
  Claude Code turn (prompt in, tool calls out, graders evaluate the
  transcript) — this is not a cheap unit test. 15 cases at whatever the
  per-case cost turns out to be; start with a ceiling, not an assumption,
  and record the observed per-run cost the first time this executes so
  future runs can budget accurately.
- `--threshold 0.8`: placeholder. Pick the real number after the first
  run establishes a baseline pass rate; 0.8 is not derived from anything
  measured.
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
2. **No documented negation primitive.** The grader-type list is
   `regex | tool_used | tool_order | file_exists | llm | baseline`.
   Nothing there is an explicit "assert this tool was NOT called."
   Every negative/absorption case (n01–n05) and every disambiguation
   case's second grader (p07–p10) expresses "skill X must not fire" as
   an `llm` grader with a `criteria` field asking a judge to fail on a
   matching Skill call. Two things need confirming once eval is live:
   (a) whether `llm` graders actually receive the full tool-call
   transcript (not just final text output) to judge against, and (b)
   whether `criteria` is the real field name for an `llm` grader — it is
   a best guess, not sourced from a schema.
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
