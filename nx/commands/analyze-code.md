---
description: Analyze codebase using codebase-deep-analyzer agent
---

# Codebase Analysis Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Project type detection
  if [ -f "pom.xml" ]; then
    echo "**Project type:** Maven"
    echo '```'
    grep -E "<artifactId>|<groupId>" pom.xml 2>/dev/null | head -4 || echo "Could not parse pom.xml"
    echo '```'
  elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
    echo "**Project type:** Gradle"
    echo '```'
    head -10 build.gradle* 2>/dev/null || echo "Could not read build.gradle"
    echo '```'
  elif [ -f "package.json" ]; then
    echo "**Project type:** Node.js"
    echo '```'
    grep -E '"name"|"version"' package.json 2>/dev/null | head -2 || echo "Could not parse package.json"
    echo '```'
  else
    echo "**Project type:** Unknown"
  fi
  echo ""

  # Module structure
  echo "### Top-level Structure"
  echo '```'
  ls -d */ 2>/dev/null | head -15 || echo "No subdirectories"
  echo '```'
  echo ""

  # Source locations
  echo "### Source Locations"
  echo '```'
  find . -type d -name "src" 2>/dev/null | grep -v node_modules | grep -v target | head -10 || echo "No src directories found"
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

## Analysis Scope

$ARGUMENTS

## Action

Invoke the **codebase-analysis** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: codebase-deep-analyzer

**Task**: Analyze codebase architecture, patterns, and dependencies
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from key entry points in project structure above]

### Analysis Scope
$ARGUMENTS

### Deliverable
Comprehensive architecture analysis: module structure map, identified design patterns, dependency graph, entry points, coding conventions, and technical debt assessment.

### Quality Criteria
- [ ] Module structure mapped with responsibilities
- [ ] Key design patterns identified and documented
- [ ] Dependencies documented (internal and external)
- [ ] Entry points identified for each module
- [ ] Coding conventions and idioms noted
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
