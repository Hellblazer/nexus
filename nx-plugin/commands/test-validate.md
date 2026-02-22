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
    FAILED=$(grep -l 'failures="[1-9]' target/surefire-reports/TEST-*.xml 2>/dev/null | wc -l | tr -d ' ')
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
    nx pm status 2>/dev/null || echo "No PM initialized"
    echo '```'
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

## Focus Area

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to test-validator:

```markdown
## Relay: test-validator

**Task**: Validate test coverage and quality for recent changes
**Bead**: [From active beads above]

### Input Artifacts
- nx store: [Search for test patterns]
- nx memory: [project/title path or 'none']
- Files: [Changed source files and their test files]

### Changes to Validate
[List from recently modified files above]

### Focus Area
$ARGUMENTS

### Deliverable
Test coverage report with gap analysis

### Quality Criteria
- [ ] All changed code has corresponding tests
- [ ] Tests cover happy path and edge cases
- [ ] Tests are meaningful (not just coverage padding)
- [ ] All tests pass
```
