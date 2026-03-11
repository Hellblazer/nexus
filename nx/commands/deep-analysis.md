---
description: Thorough analysis of complex problems using deep-analyst agent
---

# Deep Analysis Request

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

  # Active beads context
  echo "### Active Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Tip"
  echo ""
  echo "The deep-analyst uses mcp__sequential-thinking__sequentialthinking: hypothesis → evidence → evaluation → conclusion."
  echo "For cross-cutting issues, this agent explores multiple components before converging on root cause."

}

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Problem to Analyze

$ARGUMENTS

## Action

Invoke the **deep-analysis** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: deep-analyst

**Task**: Investigate "$ARGUMENTS" using hypothesis-driven sequential analysis
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from key files related to the problem]

### Problem Statement
$ARGUMENTS

### Deliverable
Root cause analysis with hypothesis chain, evidence inventory, confidence-rated conclusions, and prioritized actionable recommendations.

### Quality Criteria
- [ ] Multiple hypotheses explored and eliminated before concluding
- [ ] Evidence gathered from code, logs, and metrics
- [ ] Root cause identified with confidence rating (high/medium/low)
- [ ] Each conclusion supported by specific cited evidence
- [ ] Recommendations are actionable and prioritized by impact

**IMPORTANT**: After analysis completes, persist findings using store_put tool (or delegate to knowledge-tidier).
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
