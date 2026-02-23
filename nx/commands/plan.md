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

  if command -v nx &> /dev/null; then
    echo "### PM Context"
    nx pm status 2>/dev/null || echo "No PM initialized"
    echo ""

    PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
    if [ -n "$PROJECT" ]; then
      echo "**T2 Memory (${PROJECT}_active):**"
      echo '```'
      nx memory list --project "${PROJECT}_active" 2>/dev/null | head -8 || echo "No T2 memory"
      echo '```'
      echo ""
      echo "**Session Scratch (T1):**"
      echo '```'
      nx scratch list 2>/dev/null | head -5 || echo "No T1 scratch"
      echo '```'
    fi
  fi
}

## Feature/Task to Plan

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to strategic-planner:

```markdown
## Relay: strategic-planner

**Task**: Create comprehensive implementation plan for: $ARGUMENTS
**Bead**: [Create epic bead for this work]

### Input Artifacts
- nx store: [Search for prior architectural decisions]
- nx memory: [project/title path or 'none']
- Files: [Relevant existing code for context]

### Requirements
$ARGUMENTS

### Deliverable
Phased execution plan with beads for tracking

### Quality Criteria
- [ ] Work broken into logical phases
- [ ] Dependencies clearly identified
- [ ] Success criteria defined per phase
- [ ] Test strategy included
- [ ] Beads created for all trackable items

**IMPORTANT**: After planning completes, MUST delegate to plan-auditor for validation before implementation.
```
