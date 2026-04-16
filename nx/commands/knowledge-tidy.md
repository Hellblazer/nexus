---
description: Persist and organize knowledge into nx store using mcp__plugin_nx_nexus__nx_tidy (RDR-080)
---

# Knowledge Tidying Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  echo "### Existing Knowledge"
  echo ""
  echo "Use **store_list** tool: collection='knowledge' to list existing knowledge entries."
  echo ""

  # Active beads context
  echo "### Recently Completed Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=done --limit=5 2>/dev/null || echo "No recently completed beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Storage Standards"
  echo ""
  echo "Title conventions: research-{topic}, decision-{component}-{name}, pattern-{name}, debug-{component}-{issue}"
  echo "All entries stored via store_put tool: collection='knowledge'"
}

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Knowledge to Persist

$ARGUMENTS

## Action

Invoke the **knowledge-tidying** skill (calls `mcp__plugin_nx_nexus__nx_tidy` directly — RDR-080, no agent spawn):

```
mcp__plugin_nx_nexus__nx_tidy(
    topic="<topic from $ARGUMENTS>",
    collection="knowledge"
)
```

Then store the organized knowledge:
```
mcp__plugin_nx_nexus__store_put(
    content="<knowledge to persist>",
    collection="knowledge",
    title="<research-*|decision-*|pattern-*|debug-*>",
    tags="<meaningful tags>"
)
```

Verify searchability: `mcp__plugin_nx_nexus__search(query="<topic>", corpus="knowledge")`
