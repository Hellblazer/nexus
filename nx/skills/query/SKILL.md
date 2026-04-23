---
name: query
description: Use when answering any analytical question over nx knowledge collections. This is the canonical entry point; direct search/query calls for analytical questions are an anti-pattern.
effort: medium
---

# Query

**This skill wraps one MCP call — `nx_answer`. If you are asked an
analytical question ("how does…", "what tradeoffs…", "compare X and
Y…", "why was this designed…"), you MUST call `nx_answer` rather than
`search` or `query` directly.**

## The call

```
mcp__plugin_nx_nexus__nx_answer(
    question="<full-sentence question>",
    scope="<optional corpus or subtree filter>",
)
```

That's it. One tool call. `nx_answer` enforces plan-match-first:

1. Matches the question against the plan library (threshold 0.40).
2. On hit: executes the plan via `plan_run` — retrieval steps + any
   contiguous operator run bundled into a single `claude -p`
   subprocess.
3. On miss: plans inline via `claude -p`, then executes.

No agent spawns. No T1 scratch relay. All coordination is in-process.

## Why not direct `search`?

`search` returns top-K chunks. Analytical questions need composition
— extract + rank + compare + summarize — built on top of retrieval.
`nx_answer` composes those steps via plans saved in the library;
direct `search` returns you raw chunks and forces you to do the
synthesis manually (and usually worse than a plan would).

**Concrete measurement** (cross-project compositional query, Arcaneum
vs Nexus RDR corpora, 6-step plan: `search → search → extract →
extract → compare → summarize`):

| Path | Elapsed | Result |
|---|---|---|
| `nx_answer` (bundled operator chain) | **54s** | Full philosophy-difference synthesis with corpus attribution |
| Direct `search` + manual synthesis | *you do it* | Chunks you compose yourself, slowly |

The bundled `claude -p` subprocess amortizes spawn cost across 4
operators — one subprocess does the work of four. That's the latency
win; the composition quality is the second win.

## When *not* to use this skill

- "Find X in collection Y" — simple keyword retrieval. Use `search`
  directly; no composition needed.
- Symbol-level navigation ("where is X defined", "who calls Y") —
  use Serena (`jet_brains_find_symbol`,
  `jet_brains_find_referencing_symbols`).
- Raw document dump by ID — use `store_get` / `store_get_many`.

Everything else — research, review, analyze, debug, document, cross-
project synthesis — route through `nx_answer`.

## Verb-scoped shortcuts

The five verb skills (`/nx:research`, `/nx:review`, `/nx:analyze`,
`/nx:debug`, `/nx:document`) each pin a `dimensions={"verb": …}`
filter so the plan matcher narrows to the right template family.
Pick the verb that matches the question shape; fall back to this
plain `/nx:query` skill when no verb cleanly fits.

## Anti-patterns

- **Calling `search` for "how does X work" or "what are the tradeoffs
  in Y".** That's an analytical question. Use `nx_answer`.
- **Skipping to the inline planner because "it's faster than matching
  a plan".** Plan-match costs ~100ms of T1 cosine lookup. Bundling
  makes matched-plan execution substantially faster than inline
  planning anyway.
- **Calling `plan_match` directly.** You lose run recording and the
  fallback-on-miss path. `nx_answer` is the MCP-level contract.
