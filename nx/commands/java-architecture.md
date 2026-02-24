---
description: Design Java architecture and create phased execution plans using java-architect-planner agent
---

# Java Architecture Request

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

  # Maven/Gradle project structure
  echo "### Project Structure"
  echo '```'
  if [ -f "pom.xml" ]; then
    echo "Maven project with modules:"
    find . -name "pom.xml" -not -path "./target/*" 2>/dev/null | head -10
  elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
    echo "Gradle project"
    find . -name "build.gradle*" -not -path "./.gradle/*" -not -path "./build/*" 2>/dev/null | head -10
  else
    echo "No Maven/Gradle project detected"
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
  echo "strategic-planner -> plan-auditor -> java-architect-planner -> java-developer"
  echo ""
  echo "### Tip"
  echo ""
  echo "The agent uses nx search --corpus code --hybrid (30-50 results) for discovery,"
  echo "then LSP for precision navigation (documentSymbol, goToImplementation, findReferences)."

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

## Relay Instructions

Use the **Task tool** to delegate to java-architect-planner:

```markdown
## Relay: java-architect-planner

**Task**: Design Java architecture for: $ARGUMENTS
**Bead**: [From active epics/features above or create new]

### Input Artifacts
- nx store: [Search for prior architectural decisions on related components]
- nx memory: [project/title path or 'none']
- Files: [Key existing source files for context]

### Requirements
$ARGUMENTS

### Deliverable
Comprehensive Java architecture design with component boundaries, interfaces, phased execution plan, and risk assessment

### Quality Criteria
- [ ] All requirements addressed in design
- [ ] Component boundaries clearly defined with interfaces specified
- [ ] Integration points with existing code identified
- [ ] Phased execution plan created with beads
- [ ] Risks identified with concrete mitigations
- [ ] Design conforms to Java 24 patterns (var, modern concurrency)
- [ ] Ready for plan-auditor validation

**IMPORTANT**: After architecture is designed, MUST delegate to plan-auditor for validation before implementation begins.
```
