---
description: Debug test failures using java-debugger agent
---

# Debug Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Issue description
  if [ -n "$ARGUMENTS" ]; then
    echo "**Issue:** $ARGUMENTS"
  else
    echo "**Issue:** (analyze recent test failures)"
  fi
  echo ""

  # Check for recent test failures
  if [ -d "target/surefire-reports" ]; then
    echo "### Recent Test Failures"
    echo '```'
    # Find failed test files
    FAILURES=$(find target/surefire-reports -name "*.txt" -exec grep -l "FAILURE\|ERROR" {} \; 2>/dev/null | head -5)
    if [ -n "$FAILURES" ]; then
      echo "$FAILURES"
      echo ""
      # Show first failure details
      FIRST=$(echo "$FAILURES" | head -1)
      if [ -f "$FIRST" ]; then
        echo "--- First failure excerpt ---"
        grep -A 10 "FAILURE\|ERROR" "$FIRST" 2>/dev/null | head -15
      fi
    else
      echo "No recent failures in surefire-reports"
    fi
    echo '```'
  else
    echo "### Build Status"
    echo '```'
    echo "No surefire-reports directory (run tests first: ./mvnw test)"
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

## Relay Instructions

Use the **Task tool** to delegate to java-debugger:

```markdown
## Relay: java-debugger

**Task**: Investigate failure using hypothesis-driven debugging
**Bead**: [From active beads above or create bug bead]

### Input Artifacts
- ChromaDB: [Search for prior debugging on similar issues]
- nx memory: [project/title path or 'none']
- Files: [Relevant source and test files]

### Context
- Error message: [From test output above]
- Stack trace: [Key frames]
- Failed attempts: [What was already tried]

### Deliverable
Root cause analysis with proposed fix

### Quality Criteria
- [ ] Root cause definitively identified
- [ ] Evidence supports conclusion
- [ ] Fix addresses root cause (not symptoms)
- [ ] Regression prevention addressed
```
