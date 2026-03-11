---
description: Debug test failures using debugger agent
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
  echo "### Recent Test Failures"
  echo '```'
  if [ -d "target/surefire-reports" ]; then
    FAILURES=$(find target/surefire-reports -name "*.txt" -exec grep -l "FAILURE\|ERROR" {} \; 2>/dev/null | head -5)
    if [ -n "$FAILURES" ]; then
      echo "$FAILURES"
    else
      echo "No recent failures in surefire-reports"
    fi
  elif [ -d "reports" ]; then
    find reports -name "*.xml" 2>/dev/null | head -5
  elif [ -f "pytest.xml" ] || [ -f "test-results.xml" ]; then
    echo "Test results file found"
  else
    echo "No test output found — run the project's test command (check CLAUDE.md)"
  fi
  echo '```'

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

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Action

Invoke the **debugging** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: debugger

**Task**: Investigate failure using hypothesis-driven debugging
**Bead**: [fill from active bead above or create bug bead]

### Input Artifacts
- Files: [fill from relevant source and test files]

### Context
- Error message: [fill from test output above]
- Stack trace: [fill key frames from test output]
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
