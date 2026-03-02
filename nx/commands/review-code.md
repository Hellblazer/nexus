---
description: Review code changes using code-review-expert agent
---

# Code Review Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Git context with error handling
  if git rev-parse --git-dir > /dev/null 2>&1; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    echo "**Branch:** $BRANCH"
    echo ""

    echo "### Modified Files"
    echo '```'
    git diff --name-only HEAD 2>/dev/null | head -20 || echo "No uncommitted changes"
    echo '```'
    echo ""

    echo "### Diff Summary"
    echo '```'
    git diff --stat HEAD 2>/dev/null | tail -10 || echo "No diff available"
    echo '```'
  else
    echo "**Note:** Not a git repository"
    echo ""
    echo "### Recently Modified Files"
    echo '```'
    find . -type f -name "*.java" -mmin -60 2>/dev/null | head -10 || echo "No recent files found"
    echo '```'
  fi

  # Bead context
  echo ""
  echo "### Active Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=3 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'

  # Project context
  echo "### Project Context"
  echo ""
  if command -v nx &> /dev/null; then
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

## Focus Areas

$ARGUMENTS

## Action

Invoke the **code-review** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: code-review-expert

**Task**: Review the code changes for quality, security, and best practices
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from modified files list above]

### Deliverable
Structured code review with severity-rated findings, grouped by category (correctness, security, maintainability, performance).

### Quality Criteria
- [ ] All changed files reviewed
- [ ] Findings categorized by severity (critical, important, suggestion)
- [ ] Actionable fix recommendations for each finding

### Focus Areas
$ARGUMENTS
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
