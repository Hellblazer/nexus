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

  # Project management context
  echo "### Project Management Context"
  echo ""
  if command -v nx &> /dev/null; then
    echo "**PM Status:**"
    echo '```'
    nx pm status 2>/dev/null || echo "No PM initialized"
    echo '```'
    echo ""
    PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
    if [ -n "$PROJECT" ]; then
      echo "**T2 Memory ($PROJECT):**"
      echo '```'
      nx memory list --project "$PROJECT" 2>/dev/null | head -8 || echo "No T2 memory"
      echo '```'
      echo ""
      echo "**Session Scratch (T1):**"
      echo '```'
      nx scratch list 2>/dev/null | head -5 || echo "No T1 scratch"
      echo '```'
    fi
  fi
}

## Artifact to Critique

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to substantive-critic:

```markdown
## Relay: substantive-critic

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
