---
name: debug
description: Use when debugging a failing code path (intentionally flat — Serena handles symbol navigation) — tries the debug plan library first for per-file authoring context and summarises design intent, falls through to /nx:query if nothing matches
effort: medium
---

# debug

**You MUST call `nx_answer` when investigating *why* a code path was
designed the way it is. Direct `search` on the failing file returns
chunks; the debug plan surfaces the decision history behind those
chunks. Skipping `nx_answer` skips the design intent.**

**Note — the debug scenario is intentionally flat** (no `traverse`
step). Dev work starts from a concrete failing path; the primary
link walk is the catalog's per-file lookup (not multi-hop graph
traversal). Serena handles symbol-level navigation separately.

## The call

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "debug"},
    context=<failing_path + symptom — as JSON string if needed>,
)
```

One tool call. `nx_answer` handles match → run → record. Plan-miss
falls through to an inline `claude -p` planner.

## Required bindings

- `failing_path` — the file (or directory) where the symptom manifests.
- `symptom` — one-line description of what's failing.

## Typical intent shapes

- "debug this test failure in X.py"
- "why is this handler returning the wrong status?"
- "trace the stack of the panic in Y"

## Complementary tools

- **Serena** — for symbol-level navigation (`jet_brains_find_symbol`,
  `jet_brains_find_referencing_symbols`, etc.). The debug plan
  surfaces design context; Serena surfaces code structure. Use them
  together.
- **`/nx:debugging`** — once the design context is known, the
  hypothesis-driven debugging skill guides the iterative fix loop.

## When direct `search` is fine

A single-corpus RDR lookup — e.g. "find the RDR that covers this
module's error-handling approach" — is fine via
`mcp__plugin_nx_nexus__search(query=..., corpus="rdr__<repo>")`. Fast,
cheap, and the chunks often contain the design rationale directly.

Use this skill when: the question needs to *walk* the catalog's
per-file links (code → RDR → related RDRs) or synthesize across
multiple design notes rather than surface a single chunk.

## Anti-patterns (do not do any of these)

- **Calling `search` directly when the question needs a catalog walk
  across per-file links.** `search` returns top-K by cosine; it won't
  traverse the "this code implements that RDR" typed links. If you
  need the walk, you need `nx_answer`.
- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step, the inline-planner fallback, and use_count telemetry.
- **Expecting the debug plan to walk the full call graph.** It
  won't — that's Serena's job. The debug plan answers "what did we
  decide about this code?", not "what calls this function?".
- **Running `debug` without a `failing_path`.** No reasonable
  default; raises `PlanRunBindingError`.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
