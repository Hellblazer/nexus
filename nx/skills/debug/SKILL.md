---
name: debug
description: Use when debugging a failing code path (intentionally flat — Serena handles symbol navigation) — tries the debug plan library first for per-file authoring context and summarises design intent, falls through to /nx:query if nothing matches
effort: medium
---

# debug

Pure verb skill. One-shot: `plan_match` with `verb=debug` →
`plan_run` → return final.

**Note — the debug scenario is intentionally flat** (no `traverse`
step). Dev work starts from a concrete failing path; the primary
link walk is the catalog's per-file lookup (not multi-hop graph
traversal). Serena handles symbol-level navigation separately.

## Flow

```
plan_match(
    intent=<caller's phrasing>,
    dimensions={verb: "debug"},
    min_confidence=0.85,
    n=1,
)
→ if match: plan_run(match, bindings={failing_path: <path>, symptom: <description>})
→ else: /nx:query <caller's intent>
```

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

## Anti-patterns

- **Expecting the debug plan to walk the full call graph.** It
  won't — that's Serena's job. The debug plan answers "what did we
  decide about this code?", not "what calls this function?".
- **Running `debug` without a `failing_path`.** No reasonable
  default; raises `PlanRunBindingError`.

See `/nx:plan-first` and `docs/plan-authoring-guide.md`.
