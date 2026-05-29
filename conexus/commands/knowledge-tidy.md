---
allowed-tools: Bash
description: Persist and organize knowledge into the T3 store using mcp__plugin_conexus_nexus__nx_tidy (RDR-080)
---

# Knowledge Tidying Request

!`nx command-context knowledge-tidy`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Knowledge to Persist

$ARGUMENTS

## Action

Invoke the **knowledge-tidying** skill (calls `mcp__plugin_conexus_nexus__nx_tidy` directly — RDR-080, no agent spawn):

```
mcp__plugin_conexus_nexus__nx_tidy(
    topic="<topic from $ARGUMENTS>",
    collection="knowledge"
)
```

Then store the organized knowledge:
```
mcp__plugin_conexus_nexus__store_put(
    content="<knowledge to persist>",
    collection="knowledge",
    title="<research-*|decision-*|pattern-*|debug-*>",
    tags="<meaningful tags>"
)
```

Verify searchability: `mcp__plugin_conexus_nexus__search(query="<topic>", corpus="knowledge")`
