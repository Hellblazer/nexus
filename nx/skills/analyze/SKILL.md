---
name: analyze
description: Use when synthesising across prose and code corpora or ranking candidates by a criterion — tries the analyze plan library first (search prose + code → reference-chain traversal → rank → generate), falls through to /nx:query if nothing matches
effort: medium
---

# analyze

Pure verb skill. Routes through `nx_answer` with `dimensions={verb: "analyze"}`
so the plan-match gate narrows to analyze templates and the full trunk runs
in one tool call.

## Flow

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "analyze"},
    context=<area, criterion, limit — as JSON string if needed>,
)
```

`nx_answer` handles match → run → record. Plan-miss falls through to an
inline `claude -p` planner.

## Required bindings

- `area` — the subject area being analysed.
- `criterion` — the axis used to rank candidates (e.g. "recency",
  "citation count", "coverage breadth").

## Optional bindings

- `limit` — per-corpus result cap (`default_bindings` → 12).

## Typical intent shapes

- "compare how X is handled across ml-systems and networking"
- "survey approaches to Y"
- "rank options for Z by cost"

## Anti-patterns

- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step and the miss-path inline-planner fallback.
- **Using `analyze` when `research` would suffice.** `research` is
  single-concept; `analyze` implies cross-corpus / cross-approach
  synthesis. If the caller only wants to understand one thing, use
  `/nx:research`.
- **Using bare `criterion`.** "important" is not a criterion; pick
  an axis the ranker can actually sort by.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
