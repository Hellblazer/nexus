---
name: knowledge-tidying
description: Use when validated findings need to be consolidated in nx T3 knowledge store.
effort: low
---

**Tier-aware discipline** — apply at session start and before every major step:

1. **Read** widest → narrowest before duplicating effort:
   - T3 (cross-project): `mcp__plugin_nx_nexus__nx_answer(...)` for verb-shape questions; `mcp__plugin_nx_nexus__search(...)` for keyword lookup.
   - T2 (project): `mcp__plugin_nx_nexus__memory_search(query="<topic>", project="<repo>")`.
   - T1 (siblings, this session): `mcp__plugin_nx_nexus__scratch(action="search", query="<topic>")`.
2. **Reuse plans** before dispatching multiple agents: `mcp__plugin_nx_nexus__plan_search(query="<task>", limit=3)`.
3. **Write back at end** — findings not stored are findings lost. Pick the tier that matches the audience:
   - `mcp__plugin_nx_nexus__scratch(action="put", ..., tags="<topic>")` for sibling agents downstream THIS session (T1, narrowest scope, cheapest write).
   - `mcp__plugin_nx_nexus__memory_put(...)` for project-scoped decisions, future sessions same project (T2).
   - `mcp__plugin_nx_nexus__store_put(...)` for permanent cross-project knowledge, future sessions everywhere (T3).
   - `mcp__plugin_nx_nexus__plan_save(...)` for multi-agent pipeline outcomes (so future callers hit plan-match).

# Knowledge Tidying

Calls the `nx_tidy` MCP tool. No agent spawn needed.

```
mcp__plugin_nx_nexus__nx_tidy(topic="<topic>", collection="<collection>")
```
