---
name: why-was-this-written
description: Use when the design intent behind a failing code path must be reduced from several RDRs/notes before trusting a fix — not for dispatching a debugger, that's the debugger agent.
effort: medium
---

**Tier-aware discipline** — before starting, check T3 (`search`), T2 (`memory_search`), and T1 (`scratch` search) widest to narrowest so you don't duplicate work already done; reuse a matching plan via `plan_search` before dispatching multiple agents. Before returning, write findings back at the tier matching their audience (`scratch` for siblings this session, `memory_put` for this project, `store_put` for permanent cross-project knowledge). Full checklist: [${CLAUDE_PLUGIN_ROOT}/resources/tier-discipline.md](../../resources/tier-discipline.md) (shared across every skill that prescribes it — nexus-cnzei.6).

# why-was-this-written

**Call `nx_answer` when the design intent behind a failing path must be
reduced from several RDRs/notes** — the debug plan surfaces that
decision history. A single-corpus RDR lookup is a `search` call — see
"When direct `search` is fine" below. This skill is retrieval only — it
does not dispatch a debugger. For hypothesis-driven fault isolation, dispatch
the `debugger` agent.

**Note — the debug scenario is intentionally flat.** It runs one `query`
on the symptom text over every corpus, hydrates the hits and summarizes
them: no `traverse` step, no link walk. Serena handles symbol-level
navigation separately.

## The call

```
mcp__plugin_conexus_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "debug"},
    context=<failing path + symptom — as JSON string if needed>,
)
```

One tool call. `nx_answer` handles match → run → record. Plan-miss
falls through to an inline `claude -p` planner.

## Required bindings

- `symptom` — one-line description of what is failing. The plan searches on it, and `nx_answer` fills it from the question when you do not bind it yourself. A failing file or directory is not a binding (path scoping was removed from the plan, nexus-7y4v0): put it in the question text so it reaches the search.

## Typical intent shapes

- "debug this test failure in X.py"
- "why is this handler returning the wrong status?"
- "trace the stack of the panic in Y"

## Complementary tools

- **Serena** — for symbol-level navigation (`jet_brains_find_symbol`,
  `jet_brains_find_referencing_symbols`, etc.). The debug plan
  surfaces design context; Serena surfaces code structure. Use them
  together.
- **`debugger` agent** — once the design context is known, the
  hypothesis-driven debugger runs the iterative fix loop.

## When direct `search` is fine

A single-corpus RDR lookup — e.g. "find the RDR that covers this
module's error-handling approach" — is fine via
`mcp__plugin_conexus_nexus__search(query=..., corpus="rdr__<owner>__<embedding_model>__v1")`
(or the bare `rdr` prefix to fan out to all matching collections).
Fast, cheap, and the chunks often contain the design rationale directly.

Use this skill when: the question needs a synthesis across several
design notes and RDRs rather than a single chunk.

## Anti-patterns (do not do any of these)

- **Calling `search` directly when the answer must be reduced from
  several documents.** `search` returns top-K chunks by cosine and
  stops there. If you need the hits read and summarized together, you
  need `nx_answer`. (For a typed-link walk, `traverse` is its own tool.)
- **Calling `plan_match` directly instead of `nx_answer`.** You lose
  the record step, the inline-planner fallback, and use_count telemetry.
- **Expecting the debug plan to walk the full call graph.** It
  won't — that's Serena's job. The debug plan answers "what did we
  decide about this code?", not "what calls this function?".
- **Running `debug` with no symptom in the question.** The plan has
  nothing to search on but the question text, so a bare "debug this"
  returns noise. Name the failure and the file.

See `/conexus:plan-first` and the plan authoring guide (https://github.com/Hellblazer/nexus/blob/main/docs/plan-authoring-guide.md).
