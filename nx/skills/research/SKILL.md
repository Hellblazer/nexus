---
name: research
description: Use when doing design / architecture / planning work that walks from prose (RDRs, docs, knowledge) into the modules implementing a concept — tries the research plan library first via plan_match, falls through to /nx:query if nothing matches
effort: medium
---

# research

Pure verb skill. The body is one-shot: call `plan_match` with
`verb=research`, execute the returned plan via `plan_run`, return
the final step's result.

## Flow

```
plan_match(
    intent=<caller's phrasing>,
    dimensions={verb: "research"},
    min_confidence=0.85,
    n=1,
)
→ if match: plan_run(match, bindings={concept: <intent-derived-concept>, ...})
→ else: /nx:query <caller's intent>
```

## Required bindings

- `concept` — the central concept to research (e.g. "projection
  quality", "ICF hub detection").

## Optional bindings

- `limit` — per-corpus result cap (defaults to 10 via
  `default_bindings`).

## Typical intent shapes

- "how does X work"
- "design context for Y"
- "trace Z from spec to code"

## Anti-patterns

- **Invoking `plan_run` with a literal description as `concept`.**
  `concept` should be a noun phrase (2-5 words), not a full
  question. Extract the key noun phrase from the intent.
- **Setting `min_confidence=0` to force a match.** Below-threshold
  matches waste `plan_run` on a weakly-matching plan; defer to
  `/nx:query` instead.

See `/nx:plan-first` for the gate discipline, and
`docs/plan-authoring-guide.md` for how the research plan template
is authored.
