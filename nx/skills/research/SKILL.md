---
name: research
description: Use when doing design / architecture / planning work that walks from prose (RDRs, docs, knowledge) into the modules implementing a concept
effort: medium
---

# research

**You MUST call `nx_answer` for research questions. Direct `search`/`query`
calls for design/architecture/planning work are an anti-pattern.** The
plan library contains research-shape templates that compose retrieval +
extract + synthesis; direct search skips the composition and returns
chunks without the structure a research question needs.

## The call

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "research"},
    scope=<optional corpus / subtree filter>,
)
```

That's it. One tool call. `nx_answer` internally:

1. Matches the question against the plan library, narrowed to
   research-verb templates via the dimensional filter.
2. On hit: executes the matched template. Contiguous operator chains
   (extract → summarize, extract → rank → summarize) collapse into a
   single `claude -p` subprocess — 55-72% faster than the old per-step
   isolation while preserving or improving output quality.
3. On miss: inline `claude -p` planner decomposes the question into a DAG,
   then `plan_run` executes it.
4. Records the run to `nx_answer_runs` for observability and bumps
   `plans.use_count`/`success_count`/`failure_count` on matched plans.

## Typical intent shapes

- "how does X work"
- "design context for Y"
- "trace Z from spec to code"
- cross-project comparisons ("how does X in project A compare to X in project B")

## When direct `search` is fine

If the question is a single-corpus keyword lookup and you only need the
raw chunks — e.g. "find the RDR that defines the Voyage quota limits" —
`mcp__plugin_nx_nexus__search` is the right tool. It returns in ~1s;
`nx_answer` would pay a plan-match + execution tax for no added
composition value.

Use this skill when: the question needs *composition* across steps
(retrieve + extract + synthesize), multi-corpus alignment, or decision-
history walking through typed catalog links.

## Anti-patterns (do not do any of these)

- **Calling `search` directly for a composition-requiring research
  question.** If the answer needs extract-then-synthesize or multi-corpus
  alignment, you need `nx_answer`. (For simple single-corpus lookups,
  see "When direct `search` is fine" above — that's not an anti-pattern.)
- **Calling `plan_match` directly instead of `nx_answer`.** You lose the
  run recording, the plan-miss inline-planner fallback, and the use_count
  telemetry that tells us whether plans are actually useful.
- **Passing a narrower `dimensions` filter than `{verb: "research"}`.**
  Research plans are `scope:global` and don't pin a domain; narrowing
  further will miss.

See [`/nx:plan-first`](../plan-first/SKILL.md) for the gate discipline
across all retrieval, and `docs/plan-authoring-guide.md` for how the
research plan template is authored.
