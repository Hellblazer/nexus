---
description: Implement feature using java-developer agent
---

# Implementation Request

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

  # Check for approved plan via nx pm
  if command -v nx &> /dev/null && nx pm status &> /dev/null 2>&1; then
    echo "### Plan Status"
    echo '```'
    nx pm resume 2>&1 | head -20 || echo "No continuation context available"
    echo '```'
    echo ""
    echo "**Note:** Ensure plan has been validated by plan-auditor before implementing."
    echo ""
  else
    echo "### Plan Status"
    echo "No active project found. Run /plan first to create an implementation plan."
    echo ""
  fi

  # Bead context
  echo "### Active Work"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  # Project type
  echo "### Project Info"
  echo '```'
  if [ -f "pom.xml" ]; then
    echo "Maven project - use ./mvnw for builds"
  elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
    echo "Gradle project - use ./gradlew for builds"
  fi
  echo '```'
}

## Task to Implement

$ARGUMENTS

## Relay Instructions

**PREREQUISITE**: Plan must be validated by plan-auditor before implementation.

Use the **Task tool** to delegate to java-developer:

```markdown
## Relay: java-developer

**Task**: Implement "$ARGUMENTS" using TDD methodology
**Bead**: [Task bead from active work - must be in_progress]

### Input Artifacts
- ChromaDB: [Search for relevant patterns]
- nx memory: [project/title path or 'none']
- Files: [Existing files to modify or create location]

### Plan Context
[Reference approved plan from plan-auditor]

### Requirements
$ARGUMENTS

### Deliverable
Working implementation with tests

### Quality Criteria
- [ ] Tests written first (TDD)
- [ ] All tests pass
- [ ] Code follows project conventions
- [ ] No regressions introduced

**IMPORTANT**: After implementation completes, MUST delegate to code-review-expert for quality review.
```
