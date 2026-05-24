---
description: Constructive critique of code, plans, designs, or documentation using substantive-critic agent
---

# Deep Critique Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Git context
  if git rev-parse --git-dir > /dev/null 2>&1; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    echo "**Branch:** $BRANCH"
    echo ""

    echo "### Modified Files"
    echo '```'
    git diff --name-only HEAD 2>/dev/null | head -20 || echo "No uncommitted changes"
    echo '```'
    echo ""
  fi

  # Active beads context
  echo "### Active Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=3 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Tip"
  echo ""
  echo "The substantive-critic analyzes structure, logical consistency, completeness, and spec conformance."
  echo "Findings are prioritized: Critical > Significant > Minor."

}

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Artifact to Critique

$ARGUMENTS

## Action

Invoke the **substantive-critique** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: substantive-critic

**Task**: Provide deep constructive critique of: $ARGUMENTS
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from artifact files to critique]

### Artifact
$ARGUMENTS

### Deliverable
Structured critique with findings categorized by priority (Critical/Significant/Minor), each with specific actionable recommendations. Covers structure, logical consistency, completeness, and spec conformance.

### Quality Criteria
- [ ] Context and purpose of artifact established
- [ ] Relevant specifications and conformance criteria identified
- [ ] Structure, logic, and completeness all analyzed
- [ ] Findings prioritized by impact (Critical > Significant > Minor)
- [ ] Each recommendation is specific and actionable
- [ ] Substantive issues surfaced (not just style/surface-level)
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
