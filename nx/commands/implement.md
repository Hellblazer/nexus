---
description: Implement feature using developer agent
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

  echo "**Note:** Ensure plan has been validated by plan-auditor before implementing."
  echo ""

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
  elif [ -f "pyproject.toml" ]; then
    echo "Python project - check CLAUDE.md for build/test commands"
  elif [ -f "go.mod" ]; then
    echo "Go project - use go build/test"
  elif [ -f "Cargo.toml" ]; then
    echo "Rust project - use cargo build/test"
  elif [ -f "package.json" ]; then
    echo "Node.js/TypeScript project - check CLAUDE.md for build/test commands"
  fi
  echo '```'

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

## Task to Implement

$ARGUMENTS

## Action

**PREREQUISITE**: Plan must be validated by plan-auditor before implementation.

Invoke the **development** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: developer

**Task**: Implement "$ARGUMENTS" using TDD methodology
**Bead**: [fill from active in_progress bead above]

### Input Artifacts
- Files: [fill from existing files to modify or target package]

### Plan Context
[fill from approved plan-auditor output]

### Requirements
$ARGUMENTS

### Deliverable
Working implementation with passing tests, following TDD red-green-refactor cycle.

### Quality Criteria
- [ ] Tests written before implementation (TDD)
- [ ] All tests pass (run the project's test command from CLAUDE.md)
- [ ] Code follows project conventions (check CLAUDE.md)
- [ ] No regressions introduced in existing tests

**IMPORTANT**: After implementation completes, MUST delegate to code-review-expert for quality review.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
