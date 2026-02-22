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
}

## Analysis Scope

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to codebase-deep-analyzer:

```markdown
## Relay: codebase-deep-analyzer

**Task**: Analyze codebase to understand architecture and patterns
**Bead**: [Create analysis bead if multi-session or 'none']

### Input Artifacts
- ChromaDB: [Search for existing architecture docs]
- nx memory: [project/title path or 'none']
- Files: [Key entry points from structure above]

### Analysis Scope
$ARGUMENTS

### Deliverable
Comprehensive architecture analysis document

### Quality Criteria
- [ ] Module structure mapped
- [ ] Key patterns identified
- [ ] Dependencies documented
- [ ] Entry points identified
- [ ] Conventions noted
```
