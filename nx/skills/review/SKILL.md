---
name: review
description: Use when critiquing / auditing / reviewing a change set against decision history — tries the review plan library first (catalog lookup → decision-evolution traversal → extract → compare), falls through to /nx:query if nothing matches
effort: medium
---

# review

Pure verb skill. Routes through `nx_answer` with `dimensions={verb: "review"}`
so the plan-match gate narrows to review templates and the full trunk runs
in one tool call.

## Flow

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "review"},
    context=<changed_paths + depth, as JSON string if needed>,
)
```

`nx_answer` handles match → run → record. Plan-miss falls through to an
inline `claude -p` planner; on total miss the tool returns an error string
the caller can show to the user.

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

- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step and the miss-path inline-planner fallback.  Let
  `nx_answer` be the entry point; it's the MCP-level contract.
- **Running `review` without changed_paths.** The review template
  has no sensible default — missing `changed_paths` raises
  `PlanRunBindingError(missing=["changed_paths"])`. Surface the
  error to the user rather than passing empty list.
- **Using `review` for bug triage.** That's `/nx:debug`; the two
  verbs differ on what drives the walk (`review` starts from a
  diff, `debug` starts from a failing path).

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
