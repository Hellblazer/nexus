---
name: review
description: Use when critiquing / auditing / reviewing a change set against decision history — tries the review plan library first (catalog lookup → decision-evolution traversal → extract → compare), falls through to /nx:query if nothing matches
effort: medium
---

# review

Pure verb skill. One-shot: call `plan_match` with `verb=review`,
execute via `plan_run`, return the final step's result.

## Flow

```
plan_match(
    intent=<caller's phrasing>,
    dimensions={verb: "review"},
    min_confidence=0.85,
    n=1,
)
→ if match: plan_run(match, bindings={changed_paths: [...], depth: 1})
→ else: /nx:query <caller's intent>
```

## Required bindings

- `changed_paths` — list of paths being reviewed (e.g. the git diff
  file list).

## Optional bindings

- `depth` — traversal depth for decision-evolution walk
  (`default_bindings` → 1).

## Specialisation hook

For security-reviews, additionally pin `dimensions={domain:
"security"}`; the loader will prefer the `strategy:security` variant
when it exists, falling back to `strategy:default` otherwise.

## Typical intent shapes

- "review this change set for X"
- "did the auth middleware refactor drift from RDR-053?"
- "critique the new taxonomy assignment logic"

## Anti-patterns

- **Running `review` without changed_paths.** The review template
  has no sensible default — missing `changed_paths` raises
  `PlanRunBindingError(missing=["changed_paths"])`. Surface the
  error to the user rather than passing empty list.
- **Using `review` for bug triage.** That's `/nx:debug`; the two
  verbs differ on what drives the walk (`review` starts from a
  diff, `debug` starts from a failing path).

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
