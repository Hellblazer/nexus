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
}

## Focus Areas

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to code-review-expert:

```markdown
## Relay: code-review-expert

**Task**: Review recent code changes for quality, security, and best practices
**Bead**: [From active beads above or 'none']

### Input Artifacts
- ChromaDB: [Search for prior reviews on these files]
- nx memory: [project/title path or 'none']
- Files: [List from git diff above]

### Deliverable
Structured code review with findings categorized by severity

### Quality Criteria
- [ ] All changed files analyzed
- [ ] Security vulnerabilities flagged
- [ ] Best practices validated
- [ ] Specific remediation guidance provided

### Focus Areas
$ARGUMENTS
```
