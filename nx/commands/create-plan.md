---
description: Create implementation plan using strategic-planner agent
---

# Planning Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Git context
  if git rev-parse --git-dir > /dev/null 2>&1; then
    echo "**Branch:** $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
    echo ""
  fi

  # Existing beads context
  echo "### Existing Epics/Features"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --type=epic --limit=5 2>/dev/null || echo "No epics found"
    echo ""
    bd list --type=feature --status=open --limit=5 2>/dev/null || echo "No open features"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  # Architecture hints
  echo "### Project Structure"
  echo '```'
  if [ -f "pom.xml" ]; then
    echo "Maven project with modules:"
    find . -name "pom.xml" -not -path "./target/*" 2>/dev/null | head -10
  elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
    echo "Gradle project"
  fi
  echo '```'

}

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Feature/Task to Plan

$ARGUMENTS

## Action

Invoke the **strategic-planning** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: strategic-planner

**Task**: Create comprehensive implementation plan for: $ARGUMENTS
**Bead**: [fill from active epic bead above or create new]

### Input Artifacts
- Files: [fill from relevant existing code for context]

### Requirements
$ARGUMENTS

### Deliverable
Phased execution plan with dependency graph, success criteria per phase, test strategy, and beads created for all trackable items.

### Quality Criteria
- [ ] Work broken into logical phases with clear boundaries
- [ ] Dependencies identified and ordered correctly
- [ ] Success criteria defined per phase (measurable)
- [ ] Test strategy included for each phase
- [ ] Beads created for all trackable items

**IMPORTANT**: After planning completes, MUST call `mcp__plugin_nx_nexus__nx_plan_audit(...)` for validation before implementation.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
