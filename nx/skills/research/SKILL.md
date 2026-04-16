---
name: research
description: Use when doing design / architecture / planning work that walks from prose (RDRs, docs, knowledge) into the modules implementing a concept
effort: medium
---

# research

Pure verb skill. Routes through `nx_answer` with `dimensions={verb: "research"}`
so the plan-match gate narrows to research-verb templates and the full
trunk (match → run → record) runs in one tool call.

## Flow

```
mcp__plugin_nx_nexus__nx_answer(
    question=<caller's phrasing>,
    dimensions={"verb": "research"},
    scope=<optional corpus / subtree filter>,
)
```

`nx_answer` internally:

1. `plan_match(intent=question, dimensions={verb: "research"})` — narrowed
   to the research scenario templates.
2. On hit: `plan_run` the matched template with bindings from `context`.
3. On miss: inline `claude -p` planner decomposes the question into a DAG,
   then `plan_run` executes it.
4. Records the run to `nx_answer_runs` for observability.

## Typical intent shapes

- "how does X work"
- "design context for Y"
- "trace Z from spec to code"

## Anti-patterns

- **Calling `plan_match` directly instead of `nx_answer`.** You lose the
  record step and the miss-path inline-planner fallback.  Let `nx_answer`
  be the entry point; it's the MCP-level contract.
- **Passing a narrower `dimensions` filter than `{verb: "research"}`.**
  Research plans are `scope:global` and don't pin a domain; narrowing
  further will miss.

See [`/nx:plan-first`](../plan-first/SKILL.md) for the gate discipline
across all retrieval, and `docs/plan-authoring-guide.md` for how the
research plan template is authored.
