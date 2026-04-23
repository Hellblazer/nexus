---
name: analyze
description: Use when synthesising across prose and code corpora or ranking candidates by a criterion — tries the analyze plan library first (search prose + code → reference-chain traversal → rank → generate), falls through to /nx:query if nothing matches
effort: medium
---

# analyze

**You MUST call `nx_answer` for cross-corpus synthesis or ranking. Direct
`search` returns unstructured chunks; analytical questions need the
search → extract → rank → generate composition that the analyze plans
provide.** This is the one verb where bundling gives the biggest wins:
a 3-op chain collapses from ~45s of per-step spawns to ~15s in a single
`claude -p` call.

## The call

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "analyze"},
    context=<area, criterion, limit — as JSON string if needed>,
)
```

One tool call. `nx_answer` handles match → run → record, including the
operator-bundle optimization. Plan-miss falls through to an inline
`claude -p` planner.

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

## When direct `search` is fine

If the question is a single-corpus lookup — e.g. "find the chunks that
mention algorithm X" — use `mcp__plugin_nx_nexus__search`. Analyze
earns its latency cost when the question requires multi-corpus
alignment, ranking by a semantic criterion, or structured extraction
before synthesis.

## Anti-patterns (do not do any of these)

- **Calling `search` directly for a cross-corpus synthesis question.**
  You get top-K chunks with no composition, no cross-corpus alignment,
  no ranking. If the question requires ranking or comparing across
  multiple collections, you need `nx_answer`'s full DAG.
- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step, the inline-planner fallback, and use_count telemetry.
- **Using `analyze` when `research` would suffice.** `research` is
  single-concept; `analyze` implies cross-corpus / cross-approach
  synthesis. If the caller only wants to understand one thing, use
  `/nx:research`.
- **Using bare `criterion`.** "important" is not a criterion; pick
  an axis the ranker can actually sort by.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
