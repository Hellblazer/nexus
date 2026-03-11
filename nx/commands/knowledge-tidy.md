---
description: Persist and organize knowledge into nx store using knowledge-tidier agent
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

Invoke the **knowledge-tidying** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: knowledge-tidier

**Task**: Organize and persist knowledge about "$ARGUMENTS" into nx store (T3)
**Bead**: [fill from recently completed bead above or 'none']

### Input Artifacts
- Files: [fill from source files or documents containing findings]

### Knowledge to Organize
$ARGUMENTS

### Deliverable
Knowledge persisted to nx store T3 with correct title convention, meaningful tags, contradiction check against existing entries, and verified searchability.

### Quality Criteria
- [ ] Knowledge stored via store_put tool: collection="knowledge"
- [ ] No contradictions with existing entries (checked and resolved)
- [ ] Title follows naming convention (research-*, decision-*, pattern-*, debug-*)
- [ ] Tags are meaningful and consistent with existing tag vocabulary
- [ ] Searchable -- verified with search tool: query="topic", corpus="knowledge"
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
