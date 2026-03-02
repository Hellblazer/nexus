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

  # Project management context
  echo "### Project Management Context"
  echo ""
  if command -v nx &> /dev/null; then
    echo "**PM Status:**"
    echo '```'
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

## Action

Invoke the **java-debugging** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: java-debugger

**Task**: Investigate failure using hypothesis-driven debugging
**Bead**: [fill from active bead above or create bug bead]

### Input Artifacts
- Files: [fill from relevant source and test files]

### Context
- Error message: [fill from test output above]
- Stack trace: [fill key frames from surefire reports]
- Failed attempts: [fill what was already tried, or 'first attempt']

### Deliverable
Root cause analysis with hypothesis chain, supporting evidence, proposed fix, and regression test recommendation.

### Quality Criteria
- [ ] Root cause definitively identified with evidence
- [ ] Hypothesis chain documented (explored and eliminated alternatives)
- [ ] Fix addresses root cause (not symptoms)
- [ ] Regression test recommended to prevent recurrence
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
