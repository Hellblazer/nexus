---
description: Audit a plan using plan-auditor agent
---

# Plan Audit Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Check for active project via nx pm
  if command -v nx &> /dev/null && nx pm status &> /dev/null 2>&1; then
    echo "**Project management:** Active"
    echo ""

    echo "### Current Status"
    echo '```'
    nx pm status 2>&1
    echo '```'
    echo ""

    echo "### Continuation Context"
    echo '```'
    nx pm status 2>&1 | head -40 || echo "No PM context available"
    echo '```'
    echo ""
  else
    echo "**Project management:** Not initialized"
    echo ""
    echo "Provide the plan to audit in the arguments or reference existing documentation."
  fi

  # Bead context
  echo ""
  echo "### Related Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --type=epic --status=open --limit=3 2>/dev/null || echo "No open epics"
  else
    echo "Beads not available"
  fi
  echo '```'
}

## Plan to Audit

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to plan-auditor:

```markdown
## Relay: plan-auditor

**Task**: Validate implementation plan before execution
**Bead**: [Epic bead ID from context]

### Input Artifacts
- nx store: [Search for architectural constraints]
- nx memory: [project/title path or 'none']
- Files: [Key files referenced in plan]

### Plan to Validate
$ARGUMENTS

[Include full plan from strategic-planner or output of: nx pm status]

### Deliverable
Validation report with go/no-go decision

### Quality Criteria
- [ ] All assumptions verified against codebase
- [ ] Dependencies confirmed to exist
- [ ] Build/test commands validated
- [ ] Risks identified and acceptable
- [ ] Clear go/no-go recommendation
```
