---
description: Design architecture and create phased execution plans using architect-planner agent
---

# Architecture Request

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

  # Project structure
  echo "### Project Structure"
  echo '```'
  if [ -f "pom.xml" ]; then
    echo "Maven project with modules:"
    find . -name "pom.xml" -not -path "./target/*" 2>/dev/null | head -10
  elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
    echo "Gradle project"
    find . -name "build.gradle*" -not -path "./.gradle/*" -not -path "./build/*" 2>/dev/null | head -10
  elif [ -f "pyproject.toml" ]; then
    echo "Python project"
  elif [ -f "go.mod" ]; then
    echo "Go project"
  elif [ -f "Cargo.toml" ]; then
    echo "Rust project"
  elif [ -f "package.json" ]; then
    echo "Node.js/TypeScript project"
  else
    echo "Check CLAUDE.md for project type"
  fi
  echo '```'
  echo ""

  # Active beads context
  echo "### Active Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
    echo ""
    bd list --type=epic --limit=3 2>/dev/null || echo "No epics"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Pipeline Position"
  echo ""
  echo "strategic-planner -> plan-auditor -> architect-planner -> developer"
  echo ""
  echo "### Tip"
  echo ""
  echo "The agent uses nx search --corpus code --hybrid (30-50 results) for discovery,"
  echo "then LSP for precision navigation (documentSymbol, goToImplementation, findReferences)."

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

## Feature/Component to Architect

$ARGUMENTS

## Action

Invoke the **architecture** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: architect-planner

**Task**: Design architecture for: $ARGUMENTS
**Bead**: [fill from active epic/feature bead above or create new]

### Input Artifacts
- Files: [fill from key existing source files for context]

### Requirements
$ARGUMENTS

### Deliverable
Comprehensive architecture design with component boundaries, interface contracts, dependency graph, phased execution plan with beads, and risk assessment with mitigations.

### Quality Criteria
- [ ] All requirements addressed in design
- [ ] Component boundaries clearly defined with interface contracts
- [ ] Integration points with existing code identified
- [ ] Phased execution plan created with beads and dependencies
- [ ] Risks identified with concrete mitigations
- [ ] Design follows project conventions (check CLAUDE.md)
- [ ] Ready for plan-auditor validation

**IMPORTANT**: After architecture is designed, MUST delegate to plan-auditor for validation before implementation begins.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
