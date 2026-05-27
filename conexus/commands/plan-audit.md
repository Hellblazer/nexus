---
allowed-tools: Bash
description: Audit a plan using mcp__plugin_conexus_nexus__nx_plan_audit (RDR-080)
---

# Plan Audit Request

!`nx command-context plan-audit -- "$ARGUMENTS"`

## Plan to Audit

$ARGUMENTS

## Action

Invoke the **plan-validation** skill (calls `mcp__plugin_conexus_nexus__nx_plan_audit` directly — RDR-080, no agent spawn):

```
mcp__plugin_conexus_nexus__nx_plan_audit(
    plan_json="<serialized plan or plan description>",
    context="<codebase context relevant to the plan, if any>"
)
```

Fill `plan_json` from the plan to validate (`$ARGUMENTS` or strategic-planner output). Fill `context` from key files referenced in the plan.

Deliverable: validation report with go/no-go decision — assumption verification, dependency confirmation, build/test command validation, risk assessment.
