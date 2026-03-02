---
description: Validate tests using test-validator agent
---

# Test Validation Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Find test directories
  echo "### Test Locations"
  echo '```'
  if [ -d "src/test" ]; then
    find src/test -type f -name "*.java" 2>/dev/null | wc -l | xargs -I{} echo "{} test files in src/test"
  fi
  find . -type d \( -name "test" -o -name "tests" -o -name "__tests__" \) 2>/dev/null | grep -v node_modules | grep -v target | head -5 || echo "No test directories found"
  echo '```'
  echo ""

  # Recent changes
  if git rev-parse --git-dir > /dev/null 2>&1; then
    echo "### Recently Modified Files"
    echo '```'
    git diff --name-only HEAD~5 2>/dev/null | grep -E "\.(java|py|ts|js)$" | head -10 || echo "No recent source changes"
    echo '```'
    echo ""
  fi

  # Test results
  if [ -d "target/surefire-reports" ]; then
    echo "### Last Test Run"
    echo '```'
    TOTAL=$(find target/surefire-reports -name "TEST-*.xml" 2>/dev/null | wc -l | tr -d ' ')
    FAILED=$(find target/surefire-reports -name "TEST-*.xml" -exec grep -l 'failures="[1-9]' {} \; 2>/dev/null | wc -l | tr -d ' ')
    echo "Total test files: $TOTAL"
    echo "Files with failures: $FAILED"
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

## Focus Area

$ARGUMENTS

## Action

Invoke the **test-validation** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: test-validator

**Task**: Validate test coverage and quality for recent changes
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from changed source files and their test files above]

### Changes to Validate
[fill from recently modified files list above]

### Focus Area
$ARGUMENTS

### Deliverable
Test coverage report with gap analysis: mapping of source files to test files, identified coverage gaps, assessment of test quality (meaningful vs padding), and pass/fail status.

### Quality Criteria
- [ ] All changed source files mapped to corresponding test files
- [ ] Coverage gaps identified with specific missing scenarios
- [ ] Tests validated as meaningful (not just coverage padding)
- [ ] All tests pass (verified by running test suite)
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
