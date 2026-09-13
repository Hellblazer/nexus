---
name: knowledge-tidying
description: Use when validated findings need to be consolidated in the T3 knowledge store.
effort: low
---

**Tier-aware discipline** — before starting, check T3 (`search`), T2 (`memory_search`), and T1 (`scratch` search) widest to narrowest so you don't duplicate work already done; reuse a matching plan via `plan_search` before dispatching multiple agents. Before returning, write findings back at the tier matching their audience (`scratch` for siblings this session, `memory_put` for this project, `store_put` for permanent cross-project knowledge). Full checklist: [resources/tier-discipline.md](../../resources/tier-discipline.md) (shared across every skill that prescribes it — nexus-cnzei.6). For `memory_put` specifically: omitting `ttl` (or passing `ttl=None`) is now the record-of-record lifetime, permanent (reversed 2026-09-12, nexus-473mx); pass an explicit `ttl=N` (days) only for an interim finding that should expire.

# Knowledge Tidying

`nx_tidy` is read-only — it consolidates and reports duplicates/contradictions
in an existing topic but performs no write. Persisting the organized result is
a separate, explicit `store_put` call.

```
mcp__plugin_conexus_nexus__nx_tidy(topic="<topic>", collection="<subject>")
```

Then store the organized knowledge:

```
mcp__plugin_conexus_nexus__store_put(
    content="<knowledge to persist>",
    collection="<subject>",
    title="<research-*|decision-*|pattern-*|debug-*>",
    tags="<meaningful tags>"
)
```

Verify searchability: `mcp__plugin_conexus_nexus__search(query="<topic>", corpus="knowledge")`
