---
name: review
description: Use when critiquing / auditing / reviewing a change set against decision history — tries the review plan library first (catalog lookup → decision-evolution traversal → extract → compare), falls through to /nx:query if nothing matches
effort: medium
---

# review

**You MUST call `nx_answer` for critique/audit/review work. Direct
`search` against RDRs or code skips the decision-evolution traversal
and extract → compare pipeline that reviews actually need.**

## The call

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "review"},
    context=<changed_paths + depth, as JSON string if needed>,
)
```

One tool call. `nx_answer` handles match → run → record. Operator chains
inside review plans (extract → compare) bundle into a single `claude -p`
subprocess. Plan-miss falls through to an inline `claude -p` planner; on
total miss the tool returns an error string the caller can show to the
user.

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

## When direct `search` is fine

If you just need to find the RDR or chunk that defines something — e.g.
"show me where RDR-053 states the auth middleware contract" —
`mcp__plugin_nx_nexus__search` against `rdr__<repo>` is the right tool.
This skill earns its cost when the review has to *align* multiple
RDRs against a change set and extract drift claims.

## Anti-patterns (do not do any of these)

- **Calling `search` directly when the review requires aligning
  multiple RDRs against a change.** You see current chunks, not the
  decision-evolution traversal. If the critique needs drift analysis
  across decision history, you need `nx_answer`.
- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step, the inline-planner fallback, and use_count telemetry.
- **Running `review` without changed_paths.** The review template
  has no sensible default — missing `changed_paths` raises
  `PlanRunBindingError(missing=["changed_paths"])`. Surface the
  error to the user rather than passing empty list.
- **Using `review` for bug triage.** That's `/nx:debug`; the two
  verbs differ on what drives the walk (`review` starts from a
  diff, `debug` starts from a failing path).

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
