---
name: analyze
description: Use when synthesising across prose and code corpora or ranking candidates by a criterion — tries the analyze plan library first (search prose + code → reference-chain traversal → rank → generate), falls through to /nx:query if nothing matches
effort: medium
---

# analyze

Pure verb skill. One-shot: `plan_match` with `verb=analyze` →
`plan_run` → return final.

## Flow

```
plan_match(
    intent=<caller's phrasing>,
    dimensions={verb: "analyze"},
    min_confidence=0.85,
    n=1,
)
→ if match: plan_run(match, bindings={area: <topic>, criterion: <ranking axis>, limit: 12})
→ else: /nx:query <caller's intent>
```

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

- **Using `analyze` when `research` would suffice.** `research` is
  single-concept; `analyze` implies cross-corpus / cross-approach
  synthesis. If the caller only wants to understand one thing, use
  `/nx:research`.
- **Using bare `criterion`.** "important" is not a criterion; pick
  an axis the ranker can actually sort by.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
