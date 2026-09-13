---
name: knowledge-tidying
description: Use when validated findings need to be consolidated in the T3 knowledge store.
effort: low
---

**Tier-aware discipline** — apply at session start and before every major step: read widest → narrowest, reuse plans, write back before returning. See [resources/tier-discipline.md](../../resources/tier-discipline.md) for the full checklist (shared across every skill that prescribes it — nexus-cnzei.6). For `memory_put` specifically: omitting `ttl` (or passing `ttl=None`) is now the record-of-record lifetime, permanent (reversed 2026-09-12, nexus-473mx); pass an explicit `ttl=N` (days) only for an interim finding that should expire.

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
