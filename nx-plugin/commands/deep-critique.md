---
description: Constructive critique of code, plans, designs, or documentation using deep-critic agent
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
  echo "The deep-critic analyzes structure, logical consistency, completeness, and spec conformance."
  echo "Findings are prioritized: Critical > Significant > Minor."
}

## Artifact to Critique

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to deep-critic:

```markdown
## Relay: deep-critic

**Task**: Provide deep constructive critique of: $ARGUMENTS
**Bead**: [From active beads above or 'none']

### Input Artifacts
- nx store: [Search for specifications or prior decisions this artifact should conform to]
- nx memory: [project/title path or 'none']
- Files: [Artifact files to critique]

### Artifact
$ARGUMENTS

### Deliverable
Structured critique with findings categorized by priority (Critical/Significant/Minor) and specific actionable recommendations

### Quality Criteria
- [ ] Context and purpose of artifact established
- [ ] Relevant specifications and criteria identified
- [ ] Structure, logic, and completeness all analyzed
- [ ] Findings prioritized by impact (Critical first)
- [ ] Recommendations are specific and actionable
- [ ] Substantive issues surfaced (not just style/surface)
```
