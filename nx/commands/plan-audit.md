---
description: Audit a plan using mcp__plugin_nx_nexus__nx_plan_audit (RDR-080)
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

Invoke the **plan-validation** skill (calls `mcp__plugin_nx_nexus__nx_plan_audit` directly — RDR-080, no agent spawn):

```
mcp__plugin_nx_nexus__nx_plan_audit(
    plan_json="<serialized plan or plan description>",
    context="<codebase context relevant to the plan, if any>"
)
```

Fill `plan_json` from the plan to validate (`$ARGUMENTS` or strategic-planner output). Fill `context` from key files referenced in the plan.

Deliverable: validation report with go/no-go decision — assumption verification, dependency confirmation, build/test command validation, risk assessment.
