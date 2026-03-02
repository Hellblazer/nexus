---
description: Audit a plan using plan-auditor agent
---

# Plan Audit Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  echo "Provide the plan to audit in the arguments or reference existing documentation."
  echo ""

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

## Action

Invoke the **plan-validation** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: plan-auditor

**Task**: Validate implementation plan before execution
**Bead**: [fill from epic bead above or 'none']

### Input Artifacts
- Files: [fill from key files referenced in plan]

### Plan to Validate
$ARGUMENTS

[fill from strategic-planner output or provided plan]

### Deliverable
Validation report with go/no-go decision: assumption verification results, dependency confirmation, build/test command validation, risk assessment, and clear recommendation.

### Quality Criteria
- [ ] All assumptions verified against actual codebase state
- [ ] Dependencies confirmed to exist (classes, APIs, libraries)
- [ ] Build/test commands validated (runnable as specified)
- [ ] Risks identified with severity and mitigation status
- [ ] Clear go/no-go recommendation with rationale
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
