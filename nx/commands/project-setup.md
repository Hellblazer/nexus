---
description: Create PM infrastructure for multi-week projects using project-management-setup agent
---

# Project Setup Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Git context
  if git rev-parse --git-dir > /dev/null 2>&1; then
    echo "**Repo:** $(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || echo 'unknown')"
    echo "**Branch:** $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
    echo ""
  fi

  # Check for existing PM infrastructure
  echo "### Existing PM Infrastructure"
  echo '```'
  if command -v nx &> /dev/null; then
    nx pm status 2>/dev/null || echo "No PM infrastructure found (nx pm init not run)"
  else
    echo "nx not available"
  fi
  echo '```'
  echo ""

  # Existing epics
  echo "### Existing Epics"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --type=epic --limit=5 2>/dev/null || echo "No epics found"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Tip"
  echo ""
  echo "Use this for projects spanning more than 3 weeks. The agent creates nx pm infrastructure,"
  echo "phase beads, and T2 memory documents for session resumability."
}

## Project to Set Up

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to project-management-setup:

```markdown
## Relay: project-management-setup

**Task**: Create PM infrastructure for "$ARGUMENTS"
**Bead**: [Create epic bead for this project]

### Input Artifacts
- nx store: [Search for prior architectural decisions or related research]
- nx memory: [project/title path or 'none']
- Files: [Existing specs, READMEs, or planning docs]

### Project Description
$ARGUMENTS

### Deliverable
Complete PM infrastructure: nx pm initialized, T2 documents created (overview, continuation, phase docs), epic and phase beads created with dependencies

### Quality Criteria
- [ ] `nx pm init` completed; `nx pm status` returns project info
- [ ] T2 documents created: overview, continuation, at least one phase doc
- [ ] `nx pm status` returns actionable project context
- [ ] Epic bead created for the overall project
- [ ] Phase beads created with inter-phase dependencies
- [ ] Ready for strategic-planner to begin implementation planning

**IMPORTANT**: After setup completes, run `nx pm status` to verify the infrastructure loads correctly.
```
